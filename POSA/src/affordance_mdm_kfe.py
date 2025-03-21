# -*- coding: utf-8 -*-

# Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V. (MPG) is
# holder of all proprietary rights on this computer program.
# You can only use this computer program if you have closed
# a license agreement with MPG or you get the right to use the computer
# program from someone who is authorized to grant you that right.
# Any use of the computer program without a valid license is prohibited and
# liable to prosecution.
#
# Copyright©2020 Max-Planck-Gesellschaft zur Förderung
# der Wissenschaften e.V. (MPG). acting on behalf of its Max Planck Institute
# for Intelligent Systems. All rights reserved.
#
# Contact: ps-license@tuebingen.mpg.de

import os, sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import os.path as osp
import numpy as np
import pandas as pd
import open3d as o3d
import torch
import joblib
from tqdm import tqdm
import random
import trimesh
import glob
import yaml
import pickle
import torchgeometry as tgm
import matplotlib.cm as mpl_cm
import matplotlib.colors as mpl_colors
from omegaconf import OmegaConf
from PIL import ImageColor
from src.optimizers import optim_factory
from src import posa_utils, eulerangles, viz_utils, misc_utils, data_utils, opt_utils
from src.opt_utils import replace_object
from src.smplx2humanml import convert_smplx2humanml
from human_body_prior.body_model.body_model import BodyModel
from src.path_planning import path_planning_prox

from scipy import stats
import cv2
import argparse

from scipy.spatial.transform import Rotation as R

def load_from_yaml(filename):
    data = OmegaConf.load(filename)
    return dict(data), argparse.Namespace(**data)

POSA_DIR = os.path.dirname(os.path.dirname(__file__))


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--args_yaml", type=str, default="./POSA/cfg_files/affordance_args.yaml", help="yaml file for POSA")
    parser.add_argument("--mdm_out_dir", type=str, default="", help="Output folder for MDM meshed to SMPL")
    parser.add_argument("--scene_name", type=str, default="MPH16", help="PROX scene name")
    parser.add_argument("--specified_object", type=str, default="", help="Furniture output from ChatGPT")
    opt = parser.parse_args()

    args_yaml = opt.args_yaml
    mdm_output_dir = opt.mdm_out_dir
    scene_name = opt.scene_name

    # keyframes = [86]


    col_names = ["mpcat40index", "mpcat40", "hex", "wnsynsetkey", "nyu40", "skip", "labels"]
    mpcat40_path = os.path.join(POSA_DIR, 'mpcat40.tsv')

    df = pd.read_csv(mpcat40_path, names=range(1,12,1))
    furniture_list = np.array([c[c.find("\t")+1:c.find('#')-1] for c in df[1:][1]])
    specified_obj_idx = None


    mdm_out_npy = os.path.join(os.path.dirname(mdm_output_dir), "results.npy")
    motion_data = np.load(mdm_output_dir + "/mesh_motion.npy", allow_pickle=True).item()
    mdm_out_data = np.load(mdm_out_npy, allow_pickle=True).item()
    sample_idx = int(os.path.basename(mdm_output_dir)[6:8])
    motion_length = mdm_out_data['lengths'][sample_idx]


    args_dict, args = load_from_yaml(args_yaml)
    scene_dir = args.scene_dir
    navmesh_dir = args.navmesh_dir
    args.scene_name = scene_name
    args.specified_object = opt.specified_object
    args_dict['scene_name'] = scene_name
    args_dict['specified_object'] = opt.specified_object
    args_dict['batch_size'] = 1
    args_dict['semantics_w'] = 0.01
    batch_size = args_dict['batch_size']
    if args.use_clothed_mesh:
        args_dict['opt_pose'] = False
    args_dict['base_dir'] = osp.expandvars(args_dict.get('base_dir'))
    args_dict['ds_us_dir'] = osp.expandvars(args_dict.get('ds_us_dir'))
    args_dict['affordance_dir'] = osp.expandvars(args_dict.get('affordance_dir'))
    args_dict['model_folder'] = osp.expandvars(args_dict.get('model_folder'))
    args_dict['rp_base_dir'] = osp.expandvars(args_dict.get('rp_base_dir'))
    body_sampling_index = np.load(args.body_sampling_index)
    furniture_data = pd.read_table(args.furniture_data)
    base_dir = args_dict.get('base_dir')
    ds_us_dir = args_dict.get('ds_us_dir')
    specified_object = args_dict.get('specified_object')

    # args.show_gen_sample = True

    if args_dict['specified_object'] != '':
        specified_obj_idx = np.where(furniture_list == specified_object)[0][0]

    # Create results folders
    affordance_dir = mdm_output_dir + "_affordance"
    os.makedirs(affordance_dir, exist_ok=True)
    pkl_folder = osp.join(affordance_dir, 'pkl', args_dict.get('scene_name'))
    os.makedirs(pkl_folder, exist_ok=True)
    physical_metric_folder = osp.join(affordance_dir, 'physical_metric', args_dict.get('scene_name'))
    os.makedirs(physical_metric_folder, exist_ok=True)
    rendering_folder = osp.join(affordance_dir, 'renderings', args_dict.get('scene_name'))
    os.makedirs(rendering_folder, exist_ok=True)
    os.makedirs(osp.join(affordance_dir, 'meshes', args_dict.get('scene_name')), exist_ok=True)
    os.makedirs(osp.join(affordance_dir, 'meshes_color', args_dict.get('scene_name')), exist_ok=True)
    os.makedirs(osp.join(affordance_dir, 'meshes_clothed', args_dict.get('scene_name')), exist_ok=True)

    device = torch.device("cuda" if args_dict.get('use_cuda') else "cpu")
    dtype = torch.float32
    args_dict['device'] = device
    args_dict['dtype'] = dtype

    A_1, U_1, D_1 = posa_utils.get_graph_params(args_dict.get('ds_us_dir'), 1, args_dict['use_cuda'])
    down_sample_fn = posa_utils.ds_us(D_1).to(device)
    up_sample_fn = posa_utils.ds_us(U_1).to(device)

    A_2, U_2, D_2 = posa_utils.get_graph_params(args_dict.get('ds_us_dir'), 2, args_dict['use_cuda'])
    down_sample_fn2 = posa_utils.ds_us(D_2).to(device)
    up_sample_fn2 = posa_utils.ds_us(U_2).to(device)

    faces_arr = trimesh.load(osp.join(ds_us_dir, 'mesh_{}.obj'.format(0)), process=False).faces
    faces_arr2 = trimesh.load(osp.join(ds_us_dir, 'mesh_{}.obj'.format(2)), process=False).faces
    model = misc_utils.load_model_checkpoint(**args_dict).to(device)

    # load 3D scene
    scene = vis_o3d = None
    if args.viz or args.show_init_pos:
        scene = o3d.io.read_triangle_mesh(osp.join(base_dir, 'scenes', args_dict.get('scene_name') + '.ply'))
    scene_data = data_utils.load_scene_data(name=scene_name, sdf_dir=osp.join(base_dir, 'sdf'),
                                            **args_dict)
    
    if args.use_semantics:
        scene_semantics = scene_data['scene_semantics']
        scene_obj_ids = np.unique(scene_semantics.nonzero().detach().cpu().numpy().squeeze()).tolist()
        if args_dict['specified_object'] != '' and not specified_obj_idx in scene_obj_ids:
            print("No {} in the scene.".format(args_dict['specified_object']))
            args_dict['specified_object'] = ''

    print("Extracting Keyframes...")
    all_gen_batch = torch.tensor([]).to(device)
    obj_counts, specified_obj_counts = [], []
    all_cano_data, frames, pelvis_z, init_bps = [], [], [], []
    for frame in tqdm(range(0, motion_length, 3)):
        pkl_file_path = os.path.join(mdm_output_dir, "{:04d}.pkl".format(frame))
        pkl_file_basename = osp.splitext(osp.basename(pkl_file_path))[0]

        # if not 'rp_corey' in pkl_file_basename:
        #     continue

        vertices_clothed = None
        if args.use_clothed_mesh:
            clothed_mesh = trimesh.load(osp.join(args.rp_base_dir, pkl_file_basename + '.obj'), process=False)
            vertices_clothed = clothed_mesh.vertices

        # load pkl file
        canonical_data = data_utils.pklmdm_to_canonical(pkl_file_path, vertices_clothed=vertices_clothed, **args_dict)
        
        vertices_org, vertices_can, faces_arr, body_model, R_can, pelvis, torch_param, vertices_clothed = canonical_data
        
        pelvis_z_offset = - vertices_org.detach().cpu().numpy().squeeze()[:, 2].min()
        pelvis_z_offset = pelvis_z_offset.clip(min=0.5)
        init_body_pose = body_model.body_pose.detach().clone()
        # DownSample
        vertices_org_ds = down_sample_fn.forward(vertices_org.unsqueeze(0).permute(0, 2, 1))
        vertices_org_ds = down_sample_fn2.forward(vertices_org_ds).permute(0, 2, 1).squeeze()
        vertices_can_ds = down_sample_fn.forward(vertices_can.unsqueeze(0).permute(0, 2, 1))
        vertices_can_ds = down_sample_fn2.forward(vertices_can_ds).permute(0, 2, 1).squeeze()

        canonical_data = list(canonical_data)
        canonical_data.append(vertices_org_ds)
        canonical_data.append(vertices_can_ds)

        if args.use_semantics:
            n = 50
            selected_batch = None
            z = torch.tensor(np.random.normal(0, 1, (n, args.z_dim)).astype(np.float32)).to(device)
            gen_batches = model.decoder(z, vertices_can_ds.unsqueeze(0).expand(n, -1, -1)).detach()
            # gen_batches.shape = torch.Size([50, 655, 43])

            mode_counts = []
            specified_counts = []
            for i in range(gen_batches.shape[0]):
                x, x_semantics = data_utils.batch2features(gen_batches[i], **args_dict)
                x_semantics = np.argmax(x_semantics, axis=-1)
                not_void_and_floor = x_semantics[(x_semantics != 0) & (x_semantics != 2)]
                if len(not_void_and_floor) == 0:
                    mode_counts.append(0)
                    specified_counts.append(0)
                    continue

                if args_dict['specified_object'] != "":
                    not_void_and_floor = replace_object(not_void_and_floor.reshape(1,-1), args_dict['specified_object'])[0]

                modes = stats.mode(not_void_and_floor)
                most_common_obj_id = modes.mode[0]
                if most_common_obj_id not in scene_obj_ids:
                    mode_counts.append(0)
                    specified_counts.append(0)
                    continue
                
                mode_counts.append(modes.count[0])
                if args_dict['specified_object'] != "":
                    specified_counts.append(np.count_nonzero(not_void_and_floor == specified_obj_idx))

            obj_counts.append(np.max(mode_counts))
            selected_batch = np.argmax(mode_counts)
            if args_dict['specified_object'] != "":
                specified_obj_counts.append(np.max(specified_counts))
                selected_batch = np.argmax(specified_counts)

            gen_batches = gen_batches[selected_batch].unsqueeze(0)

        else:
            z = torch.tensor(np.random.normal(0, 1, (args.num_rendered_samples, args.z_dim)).astype(np.float32)).to(device)
            gen_batches = model.decoder(z, vertices_can_ds.unsqueeze(0).expand(args.num_rendered_samples, -1, -1)).detach()

        all_gen_batch = torch.concat([all_gen_batch, gen_batches])
        all_cano_data.append(canonical_data)
        pelvis_z.append(pelvis_z_offset)
        init_bps.append(init_body_pose)
        frames.append(frame)

        if args.show_gen_sample:
            gen = gen_batches.clone()
            gen_batch_us = up_sample_fn2.forward(gen.transpose(1, 2))
            gen_batch_us = up_sample_fn.forward(gen_batch_us).transpose(1, 2)
            gen_sample_img = viz_utils.render_sample(gen_batch_us, vertices_org, faces_arr, **args_dict)[0]
            gen_sample_img.save(osp.join(affordance_dir, 'renderings', args_dict.get('scene_name'), pkl_file_basename + '_gen.png'))

    if args_dict['specified_object'] != "" and np.max(specified_obj_counts) > 20:
        max_ind = np.argmax(specified_obj_counts)
    else:
        max_ind = np.argmax(obj_counts)
    print("Keyframe is {}.".format(frames[max_ind]))

    vertices_org, vertices_can, faces_arr, body_model, R_can, pelvis, torch_param, vertices_clothed, vertices_org_ds, vertices_can_ds = all_cano_data[max_ind]
    pelvis_z_offset = pelvis_z[max_ind]
    init_body_pose = init_bps[max_ind]
    keyframes = [frames[max_ind]]

    del all_cano_data

    result_filename = '{:03d}'.format(frames[max_ind])
    gen_batch = all_gen_batch[max_ind, :, :].unsqueeze(0)

    if args.show_gen_sample:
        gen = gen_batch.clone()
        gen_batch_us = up_sample_fn2.forward(gen.transpose(1, 2))
        gen_batch_us = up_sample_fn.forward(gen_batch_us).transpose(1, 2)
        if args.viz:
            gen = viz_utils.show_sample(vertices_org, gen_batch_us, faces_arr, **args_dict)
            o3d.visualization.draw_geometries(gen)
        if args.render:
            gen_sample_img = viz_utils.render_sample(gen_batch_us, vertices_org, faces_arr, **args_dict)[0]
            gen_sample_img.save(osp.join(affordance_dir, 'renderings', args_dict.get('scene_name'),
                                            pkl_file_basename + '_gen.png'))

    # Create init points grid
    init_pos = torch.tensor(
        misc_utils.create_init_points(scene_data['bbox'].detach().cpu().numpy(), args.affordance_step,
                                        pelvis_z_offset), dtype=dtype, device=device).reshape(-1, 1, 3)
    if args.show_init_pos:
        points = [scene]
        for i in range(len(init_pos)):
            points.append(
                viz_utils.create_o3d_sphere(init_pos[i].detach().cpu().numpy().squeeze(), radius=0.03))
        o3d.visualization.draw_geometries(points)
    # Eval init points
    init_pos, init_ang = opt_utils.init_points_culling(init_pos=init_pos, vertices=vertices_org_ds,
                                                        scene_data=scene_data, gen_batch=gen_batch, **args_dict)

    if args.show_init_pos:
        points = []
        vertices_np = vertices_org.detach().cpu().numpy()
        bodies = []
        for i in range(len(init_pos)):
            points.append(
                viz_utils.create_o3d_sphere(init_pos[i].detach().cpu().numpy().squeeze(), radius=0.03))
            rot_aa = torch.cat((torch.zeros((1, 2), device=device), init_ang[i].reshape(1, 1)), dim=1)
            rot_mat = tgm.angle_axis_to_rotation_matrix(rot_aa.reshape(-1, 3))[:, :3,
                        :3].detach().cpu().numpy().squeeze()
            v = np.matmul(rot_mat, vertices_np.transpose()).transpose() + init_pos[i].detach().cpu().numpy()
            body = viz_utils.create_o3d_mesh_from_np(vertices=v, faces=faces_arr)
            bodies.append(body)

        # o3d.visualization.draw_geometries(points + [scene])
        visualizer = o3d.visualization.Visualizer()
        visualizer.create_window()
        for p in points:
            visualizer.add_geometry(p)
        for b in bodies:
            visualizer.add_geometry(b)
        visualizer.add_geometry(scene)
        visualizer.run()
        # visualizer.destroy_window()

    ###########################################################################################################
    #####################            Start of Optimization Loop                  ##############################
    ###########################################################################################################
    results = []
    results_clothed = []
    rot_mats, ts = [], []
    losses = []

    for i in tqdm(range(init_pos.shape[0])):
        body_model.reset_params(**torch_param)
        t_free = init_pos[i].reshape(1, 1, 3).clone().detach().requires_grad_(True)
        ang_free = init_ang[i].reshape(1, 1).clone().detach().requires_grad_(True)

        free_param = [t_free, ang_free]
        if args.opt_pose:
            free_param += [body_model.body_pose]
        optimizer, _ = optim_factory.create_optimizer(free_param, optim_type='lbfgsls',
                                                        lr=args_dict.get('affordance_lr'), ftol=1e-9,
                                                        gtol=1e-9,
                                                        max_iter=args.max_iter)

        opt_wrapper_obj = opt_utils.opt_wrapper(vertices=vertices_org_ds.unsqueeze(0),
                                                vertices_can=vertices_can_ds, pelvis=pelvis,
                                                scene_data=scene_data,
                                                down_sample_fn=down_sample_fn, down_sample_fn2=down_sample_fn2,
                                                optimizer=optimizer, gen_batch=gen_batch, body_model=body_model,
                                                init_body_pose=init_body_pose,
                                                **args_dict)

        closure = opt_wrapper_obj.create_fitting_closure(t_free, ang_free)
        for _ in range(10):
            loss = optimizer.step(closure)
        # Get body vertices after optimization
        curr_results, rot_mat = opt_wrapper_obj.compute_vertices(t_free, ang_free,
                                                                    vertices=vertices_org.unsqueeze(0),
                                                                    down_sample=False)

        if torch.is_tensor(loss):
            loss = float(loss.detach().cpu().squeeze().numpy())
        losses.append(loss)
        results.append(curr_results.squeeze().detach().cpu().numpy())

        # Get clothed body vertices after optimization
        if vertices_clothed is not None:
            curr_results_clothed, rot_mat = opt_wrapper_obj.compute_vertices(t_free, ang_free,
                                                                                vertices=vertices_clothed.unsqueeze(
                                                                                    0),
                                                                                down_sample=False)
            results_clothed.append(curr_results_clothed.squeeze().detach().cpu().numpy())

        rot_mats.append(rot_mat)
        ts.append(t_free)

    ###########################################################################################################
    #####################            End of Optimization Loop                  ################################
    ###########################################################################################################

    losses = np.array(losses)
    if len(losses > 0):
        idx = losses.argmin()
        print('minimum final loss = {}'.format(losses[idx]))
        sorted_ind = np.argsort(losses)
        for i in range(min(args.num_rendered_samples, len(losses))):
            ind = sorted_ind[i]
            cm = mpl_cm.get_cmap('Reds')
            norm = mpl_colors.Normalize(vmin=0.0, vmax=1.0)
            colors = cm(norm(losses))

            rot_mat = rot_mats[ind]
            t_free = ts[ind]

            ## Save pickle
            R_smpl2scene = torch.tensor(eulerangles.euler2mat(np.pi / 2, 0, 0, 'sxyz'), dtype=dtype,
                                        device=device)
            Rcw = torch.matmul(rot_mat.reshape(3, 3), R_smpl2scene)
            t_before = torch_param['transl']
            torch_param = misc_utils.smpl_in_new_coords(torch_param, Rcw, t_free.reshape(1, 3),
                                                        rotation_center=pelvis, **args_dict)
            param = {}
            for key in torch_param.keys():
                param[key] = torch_param[key].detach().cpu().numpy()
            param['R_fm_orig'] = rot_mat.detach().cpu().numpy()[0]
            param['aa_fm_orig'] = cv2.Rodrigues(rot_mat.detach().cpu().squeeze().numpy())[0]
            param['t_fm_orig'] = t_free.detach().cpu().numpy()[0]
            param['keyframes'] = keyframes
            param['length'] = motion_length
            param['motion'] = motion_data['poses']
            param['scene_name'] = scene_name
            save_pkl_path = osp.join(pkl_folder, '{}.pkl'.format(result_filename))
            with open(save_pkl_path, 'wb') as f:
                pickle.dump(param, f)

            # path planning
            path = path_planning_prox(save_pkl_path,
                                        navmesh_dir=navmesh_dir,
                                        scene_dir=scene_dir,
                                        scale_factor=0.4 )
            

            # # calculate initial orient
            # A = path[1:, [0,1]] - path[:-1, [0,1]]
            # cos_theta = np.dot(A, np.array([0, -1])) / np.linalg.norm(A, axis=1)
            # angles = np.arccos(cos_theta)
            # for i in range(1, len(angles)):
            #     if np.isnan(angles[i]):
            #         angles[i] = angles[i - 1]
            # angles = np.append(angles, angles[-1])
            orient = np.zeros([motion_length, 3])
            orient[:, 0] += np.pi / 2
            # orient[:, 2] = angles
            # rot_matrix = R.from_euler('xyz', orient).as_matrix()
            # orient = R.from_matrix(rot_matrix).as_euler('xyz')


            data = {}
            data['global_orient']   = orient   
            data['betas']           = np.zeros([motion_length, 10])     
            data['body_pose']       = np.zeros([motion_length, 63])       
            data['transl']          = path
            data['left_hand_pose']  = np.zeros([motion_length, 6])             
            data['right_hand_pose'] = np.zeros([motion_length, 6])             
            data['R_fm_orig']       = param['R_fm_orig']            
            data['t_fm_orig']       = param['t_fm_orig']        
            data['keyframes']       = param['keyframes']        
            data['length']          = motion_length  
            data['scene_name']      = param['scene_name']
            data['text']            = mdm_out_data['text'][sample_idx]

            # Insert key pose
            for kf in param['keyframes']:
                data['global_orient'][kf]   = torch_param['global_orient'][0].detach().cpu().numpy()
                data['body_pose'][kf]       = torch_param['body_pose'][0].detach().cpu().numpy()

            # Convert to Humanml3D's canonical coordinate
            convert_smplx2humanml(device, 
                                  data, 
                                  save_pkl_path[:-4] + ".npy", 
                                  flip=False)

            # Evaluate Physical Metric
            gen_batch_us = up_sample_fn2.forward(gen_batch.transpose(1, 2))
            gen_batch_us = up_sample_fn.forward(gen_batch_us).transpose(1, 2)

            non_collision_score, contact_score = misc_utils.eval_physical_metric(
                torch.tensor(results[ind], dtype=dtype, device=device).unsqueeze(0),
                scene_data)
            try:
                with open(osp.join(physical_metric_folder, '{}.yaml'.format(result_filename)), 'w') as f:
                    yaml.dump({'non_collision_score': non_collision_score,
                            'contact_score': contact_score},
                            f)
            except Exception:
                pass

            if args.viz:
                bodies = [scene]
                if args.use_clothed_mesh:
                    body = viz_utils.create_o3d_mes0h_from_np(vertices=results_clothed[ind],
                                                                faces=clothed_mesh.faces)

                else:
                    body = viz_utils.create_o3d_mesh_from_np(vertices=results[ind], faces=faces_arr)
                bodies.append(body)
                o3d.visualization.draw_geometries(bodies)

            if args.render or args.save_meshes:
                default_vertex_colors = np.ones((results[ind].shape[0], 3)) * np.array(viz_utils.default_color)
                body = trimesh.Trimesh(results[ind], faces_arr, vertex_colors=default_vertex_colors, process=False)
                
                targets = gen_batch[:, :, 1:].argmax(dim=-1).type(torch.long).reshape(batch_size, -1)[0].detach().cpu().numpy()
                rgb_colors = np.array([ImageColor.getcolor(hex, "RGB") 
                    for hex in [furniture_data[furniture_data['mpcat40index']==label]['hex'].iloc[0] 
                        for label in targets]])
                targets = gen_batch[:, :, 1:].argmax(dim=-1).type(torch.long).reshape(batch_size, -1)[0].detach().cpu().numpy()
                color_body = trimesh.Trimesh(results[ind][body_sampling_index], faces_arr2, vertex_colors=rgb_colors, process=False)
                
                clothed_body = None
                if args.use_clothed_mesh:
                    clothed_body = trimesh.Trimesh(results_clothed[ind], clothed_mesh.faces, process=False)

                if args.save_meshes:
                    body.export(osp.join(affordance_dir, 'meshes', args_dict.get('scene_name'),
                                            '{}_{}.obj'.format(result_filename, i)))
                    
                    color_body.export(osp.join(affordance_dir, 'meshes_color', args_dict.get('scene_name'),
                                            '{}_{}.obj'.format(result_filename, i)))
                    
                if args_dict['specified_object'] != '':
                    targets = replace_object(targets, args_dict['specified_object'])
                    rgb_colors = np.array([ImageColor.getcolor(hex, "RGB") 
                        for hex in [furniture_data[furniture_data['mpcat40index']==label]['hex'].iloc[0] 
                            for label in targets]])
                    color_body = trimesh.Trimesh(results[ind][body_sampling_index], faces_arr2, vertex_colors=rgb_colors, process=False)
                    color_body.export(osp.join(affordance_dir, 'meshes_color', args_dict.get('scene_name'),
                                            '{}_{}_{}.obj'.format(result_filename, i, args_dict['specified_object'])))

                    if args.use_clothed_mesh:
                        clothed_body.export(
                            osp.join(affordance_dir, 'meshes_clothed', args_dict.get('scene_name'),
                                        '{}_{}.obj'.format(result_filename, i)))

                # Update mdm output file
                mdm_out_data['t_fm_orig'][sample_idx] = param['t_fm_orig']
                mdm_out_data['R_fm_orig'][sample_idx] = param['R_fm_orig']
                mdm_out_data['keyframes'][sample_idx] = param['keyframes']
                np.save(mdm_out_npy, mdm_out_data)


                if args.render:
                    scene_mesh = trimesh.load(
                        osp.join(base_dir, 'scenes', args_dict.get('scene_name') + '.ply'))
                    img_collage = viz_utils.render_interaction_snapshot(body, scene_mesh, clothed_body,
                                                                        body_center=True,
                                                                        collage_mode='horizantal', **args_dict)
                    img_collage.save(osp.join(rendering_folder, '{}.png'.format(result_filename)))
