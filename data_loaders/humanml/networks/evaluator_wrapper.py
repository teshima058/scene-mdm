from data_loaders.humanml.networks.modules import *
from data_loaders.humanml.utils.word_vectorizer import POS_enumerator
from os.path import join as pjoin
from data_loaders.humanml.scripts.motion_process import recover_from_ric, convert_humanml_to_266, recover_from_ric_from_266


def motion_wo_foot_contact(motion, foot_contact_entries):
    if foot_contact_entries == 0:
        return motion
    else:
        return motion[..., :-foot_contact_entries]


def build_models(opt):
    movement_enc = MovementConvEncoder(opt.dim_pose-opt.foot_contact_entries, opt.dim_movement_enc_hidden, opt.dim_movement_latent)
    text_enc = TextEncoderBiGRUCo(word_size=opt.dim_word,
                                  pos_size=opt.dim_pos_ohot,
                                  hidden_size=opt.dim_text_hidden,
                                  output_size=opt.dim_coemb_hidden,
                                  device=opt.device)

    motion_enc = MotionEncoderBiGRUCo(input_size=opt.dim_movement_latent,
                                      hidden_size=opt.dim_motion_hidden,
                                      output_size=opt.dim_coemb_hidden,
                                      device=opt.device)

    checkpoint = torch.load(pjoin(opt.checkpoints_dir, opt.dataset_name, 'text_mot_match', 'model', 'finest.tar'),
                            map_location=opt.device)
    movement_enc.load_state_dict(checkpoint['movement_encoder'])
    text_enc.load_state_dict(checkpoint['text_encoder'])
    motion_enc.load_state_dict(checkpoint['motion_encoder'])
    print('Loading Evaluation Model Wrapper (Epoch %d) Completed!!' % (checkpoint['epoch']))
    return text_enc, motion_enc, movement_enc


class EvaluatorModelWrapper(object):

    def __init__(self, opt):

        if opt.dataset_name == 't2m':
            opt.dim_pose = 263
        elif opt.dataset_name == 'kit':
            opt.dim_pose = 251
        else:
            raise KeyError('Dataset not Recognized!!!')

        opt.dim_word = 300
        opt.max_motion_length = 196
        opt.dim_pos_ohot = len(POS_enumerator)
        opt.dim_motion_hidden = 1024
        opt.max_text_len = 20
        opt.dim_text_hidden = 512
        opt.dim_coemb_hidden = 512

        self.text_encoder, self.motion_encoder, self.movement_encoder = build_models(opt)
        self.opt = opt
        self.device = opt.device

        self.text_encoder.to(opt.device)
        self.motion_encoder.to(opt.device)
        self.movement_encoder.to(opt.device)

        self.text_encoder.eval()
        self.motion_encoder.eval()
        self.movement_encoder.eval()

    # Please note that the results does not following the order of inputs
    def get_co_embeddings(self, word_embs, pos_ohot, cap_lens, motions, m_lens):
        with torch.no_grad():
            word_embs = word_embs.detach().to(self.device).float()
            pos_ohot = pos_ohot.detach().to(self.device).float()
            motions = motions.detach().to(self.device).float()

            align_idx = np.argsort(m_lens.data.tolist())[::-1].copy()
            motions = motions[align_idx]
            m_lens = m_lens[align_idx]

            '''Movement Encoding'''
            movements = self.movement_encoder(motion_wo_foot_contact(motions, self.opt.foot_contact_entries)).detach()
            m_lens = m_lens // self.opt.unit_length
            motion_embedding = self.motion_encoder(movements, m_lens)

            '''Text Encoding'''
            text_embedding = self.text_encoder(word_embs, pos_ohot, cap_lens)
            text_embedding = text_embedding[align_idx]
        return text_embedding, motion_embedding

    # Please note that the results does not following the order of inputs
    def get_motion_embeddings(self, motions, m_lens):
        with torch.no_grad():
            motions = motions.detach().to(self.device).float()

            align_idx = np.argsort(m_lens.data.tolist())[::-1].copy()
            motions = motions[align_idx]
            m_lens = m_lens[align_idx]

            '''Movement Encoding'''
            movements = self.movement_encoder(motion_wo_foot_contact(motions, self.opt.foot_contact_entries)).detach()
            m_lens = m_lens // self.opt.unit_length
            motion_embedding = self.motion_encoder(movements, m_lens)
        return motion_embedding

# our version
def build_evaluators(opt):
    movement_enc = MovementConvEncoder(opt['dim_pose']-opt['foot_contact_entries'], opt['dim_movement_enc_hidden'], opt['dim_movement_latent'])
    text_enc = TextEncoderBiGRUCo(word_size=opt['dim_word'],
                                  pos_size=opt['dim_pos_ohot'],
                                  hidden_size=opt['dim_text_hidden'],
                                  output_size=opt['dim_coemb_hidden'],
                                  device=opt['device'])

    motion_enc = MotionEncoderBiGRUCo(input_size=opt['dim_movement_latent'],
                                      hidden_size=opt['dim_motion_hidden'],
                                      output_size=opt['dim_coemb_hidden'],
                                      device=opt['device'])

    ckpt_dir = opt['dataset_name']
    if opt['dataset_name'] == 'humanml' or opt['dataset_name'] == 'humanml_266' or opt['dataset_name'] == 'humanml_mask':
        ckpt_dir = 't2m_66'

    model_name = 'text_mot_match'
    if opt['dataset_name'] == 'babel':
        # model_name = 'text_mot_match_babel_bs64_pretrained_latest_rerun'
        model_name = 'text_mot_match_babel_random_motion_bs64'
    opt['checkpoints_dir'] = './dataset'

    checkpoint = torch.load(pjoin(opt['checkpoints_dir'], ckpt_dir, model_name, 'model', 'finest.tar'),
                            map_location=opt['device'])
    movement_enc.load_state_dict(checkpoint['movement_encoder'])
    text_enc.load_state_dict(checkpoint['text_encoder'])
    motion_enc.load_state_dict(checkpoint['motion_encoder'])
    print('Loading Evaluation Model Wrapper (Epoch %d) Completed!!' % (checkpoint['epoch']))
    return text_enc, motion_enc, movement_enc

# our wrapper
class EvaluatorMDMWrapper(object):

    def __init__(self, dataset_name, device):
        opt = {
            'dataset_name': dataset_name,
            'device': device,
            'dim_word': 300,
            'max_motion_length': 196,
            'dim_pos_ohot': len(POS_enumerator),
            'dim_motion_hidden': 1024,
            'max_text_len': 20,
            'dim_text_hidden': 512,
            'dim_coemb_hidden': 512,
            'dim_pose': 263 if dataset_name in ['humanml', 'humanml_266', 'humanml_mask'] else 251,
            'dim_movement_enc_hidden': 512,
            'dim_movement_latent': 512,
            'checkpoints_dir': '.',
            'unit_length': 4,
            'foot_contact_entries': 4,
        }

        if opt['dataset_name'] == 'babel':
            opt['dim_pose'] = 135
            opt['foot_contact_entries'] = 0

        elif opt['dataset_name'] in ['humanml', 'humanml_266', 'humanml_mask']:
            opt['dim_pose'] = 66
            opt['foot_contact_entries'] = 0
            self.mean = np.load("../motion-diffusion-model/dataset/HumanML3D/Mean_66.npy")
            self.std = np.load("../motion-diffusion-model/dataset/HumanML3D/Std_66.npy")
            
        self.text_encoder, self.motion_encoder, self.movement_encoder = build_evaluators(opt)
        self.opt = opt
        self.device = opt['device']

        self.text_encoder.to(opt['device'])
        self.motion_encoder.to(opt['device'])
        self.movement_encoder.to(opt['device'])

        self.text_encoder.eval()
        self.motion_encoder.eval()
        self.movement_encoder.eval()

    # Please note that the results does not following the order of inputs
    def get_co_embeddings(self, word_embs, pos_ohot, cap_lens, motions, m_lens, data):
        with torch.no_grad():
            word_embs = word_embs.detach().to(self.device).float()
            pos_ohot = pos_ohot.detach().to(self.device).float()
            motions = motions.detach().to(self.device).float()

            align_idx = np.argsort(m_lens.data.tolist())[::-1].copy()
            motions = motions[align_idx]
            m_lens = m_lens[align_idx]

            '''Movement Encoding'''
            if motions.shape[-1] == 267:
                motions = motions[:,...,:-1]
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric_from_266(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            elif motions.shape[-1] == 266:
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric_from_266(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            elif motions.shape[-1] == 263:
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            else:
                motions = motion_wo_foot_contact(motions, self.opt['foot_contact_entries'])

            movements = self.movement_encoder(motions).detach()
            m_lens = m_lens // self.opt['unit_length']
            motion_embedding = self.motion_encoder(movements, m_lens)

            '''Text Encoding'''
            text_embedding = self.text_encoder(word_embs, pos_ohot, cap_lens)
            text_embedding = text_embedding[align_idx]
        return text_embedding, motion_embedding

    # Please note that the results does not following the order of inputs
    def get_motion_embeddings(self, motions, m_lens, data):
        with torch.no_grad():
            motions = motions.detach().to(self.device).float()

            align_idx = np.argsort(m_lens.data.tolist())[::-1].copy()
            motions = motions[align_idx]
            m_lens = m_lens[align_idx]

            '''Movement Encoding'''
            if motions.shape[-1] == 267:
                motions = motions[:,...,:-1]
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric_from_266(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            elif motions.shape[-1] == 266:
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric_from_266(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            elif motions.shape[-1] == 263:
                motions = data.dataset.t2m_dataset.inv_transform(motions.cpu()).float()
                motions = recover_from_ric(motions, joints_num=22)
                motions = motions.reshape(motions.shape[0], motions.shape[1], -1)
                motions = ((motions - self.mean) / self.std).to(self.device).float()
            else:
                motions = motion_wo_foot_contact(motions, self.opt['foot_contact_entries'])
            movements = self.movement_encoder(motions).detach()
            m_lens = m_lens // self.opt['unit_length']
            motion_embedding = self.motion_encoder(movements, m_lens)
        return motion_embedding