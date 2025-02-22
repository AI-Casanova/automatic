import typing
import os
import re
import math
import time
import inspect
import torch
import numpy as np
from modules import shared, errors, sd_models, processing, processing_vae, processing_helpers, sd_hijack_hypertile, prompt_parser_diffusers, timer
from modules.processing_callbacks import diffusers_callback_legacy, diffusers_callback, set_callbacks_p
from modules.processing_helpers import resize_hires, fix_prompts, calculate_base_steps, calculate_hires_steps, calculate_refiner_steps, get_generator, set_latents, apply_circular # pylint: disable=unused-import


debug = shared.log.trace if os.environ.get('SD_DIFFUSERS_DEBUG', None) is not None else lambda *args, **kwargs: None


def task_specific_kwargs(p, model):
    task_args = {}
    is_img2img_model = bool('Zero123' in shared.sd_model.__class__.__name__)
    if len(getattr(p, 'init_images', [])) > 0:
        p.init_images = [p.convert('RGB') for p in p.init_images]
    if sd_models.get_diffusers_task(model) == sd_models.DiffusersTaskType.TEXT_2_IMAGE or len(getattr(p, 'init_images', [])) == 0 and not is_img2img_model:
        p.ops.append('txt2img')
        if hasattr(p, 'width') and hasattr(p, 'height'):
            task_args = {
                'width': 8 * math.ceil(p.width / 8),
                'height': 8 * math.ceil(p.height / 8),
            }
    elif (sd_models.get_diffusers_task(model) == sd_models.DiffusersTaskType.IMAGE_2_IMAGE or is_img2img_model) and len(getattr(p, 'init_images', [])) > 0:
        if shared.sd_model_type == 'sdxl':
            model.register_to_config(requires_aesthetics_score = False)
        p.ops.append('img2img')
        task_args = {
            'image': p.init_images,
            'strength': p.denoising_strength,
        }
        if model.__class__.__name__ == 'FluxImg2ImgPipeline': # needs explicit width/height
            p.width = 8 * math.ceil(p.init_images[0].width / 8)
            p.height = 8 * math.ceil(p.init_images[0].height / 8)
            task_args['width'], task_args['height'] = p.width, p.height
        if model.__class__.__name__ == 'OmniGenPipeline':
            p.width = 16 * math.ceil(p.init_images[0].width / 16)
            p.height = 16 * math.ceil(p.init_images[0].height / 16)
            task_args = {
                'width': p.width,
                'height': p.height,
                'input_images': [p.init_images], # omnigen expects list-of-lists
            }
    elif sd_models.get_diffusers_task(model) == sd_models.DiffusersTaskType.INSTRUCT and len(getattr(p, 'init_images', [])) > 0:
        p.ops.append('instruct')
        task_args = {
            'width': 8 * math.ceil(p.width / 8) if hasattr(p, 'width') else None,
            'height': 8 * math.ceil(p.height / 8) if hasattr(p, 'height') else None,
            'image': p.init_images,
            'strength': p.denoising_strength,
        }
    elif (sd_models.get_diffusers_task(model) == sd_models.DiffusersTaskType.INPAINTING or is_img2img_model) and len(getattr(p, 'init_images', [])) > 0:
        if shared.sd_model_type == 'sdxl':
            model.register_to_config(requires_aesthetics_score = False)
        if p.detailer:
            p.ops.append('detailer')
        else:
            p.ops.append('inpaint')
        width, height = processing_helpers.resize_init_images(p)
        task_args = {
            'image': p.init_images,
            'mask_image': p.task_args.get('image_mask', None) or getattr(p, 'image_mask', None) or getattr(p, 'mask', None),
            'strength': p.denoising_strength,
            'height': height,
            'width': width,
        }
    if model.__class__.__name__ == 'LatentConsistencyModelPipeline' and hasattr(p, 'init_images') and len(p.init_images) > 0:
        p.ops.append('lcm')
        init_latents = [processing_vae.vae_encode(image, model=shared.sd_model, full_quality=p.full_quality).squeeze(dim=0) for image in p.init_images]
        init_latent = torch.stack(init_latents, dim=0).to(shared.device)
        init_noise = p.denoising_strength * processing.create_random_tensors(init_latent.shape[1:], seeds=p.all_seeds, subseeds=p.all_subseeds, subseed_strength=p.subseed_strength, p=p)
        init_latent = (1 - p.denoising_strength) * init_latent + init_noise
        task_args = {
            'latents': init_latent.to(model.dtype),
            'width': p.width if hasattr(p, 'width') else None,
            'height': p.height if hasattr(p, 'height') else None,
        }
    if model.__class__.__name__ == 'BlipDiffusionPipeline':
        if len(getattr(p, 'init_images', [])) == 0:
            shared.log.error('BLiP diffusion requires init image')
            return task_args
        task_args = {
            'reference_image': p.init_images[0],
            'source_subject_category': getattr(p, 'negative_prompt', '').split()[-1],
            'target_subject_category': getattr(p, 'prompt', '').split()[-1],
            'output_type': 'pil',
        }
    debug(f'Diffusers task specific args: {task_args}')
    return task_args


def set_pipeline_args(p, model, prompts: list, negative_prompts: list, prompts_2: typing.Optional[list]=None, negative_prompts_2: typing.Optional[list]=None, desc:str='', **kwargs):
    t0 = time.time()
    apply_circular(p.tiling, model)
    if hasattr(model, "set_progress_bar_config"):
        model.set_progress_bar_config(bar_format='Progress {rate_fmt}{postfix} {bar} {percentage:3.0f}% {n_fmt}/{total_fmt} {elapsed} {remaining} ' + '\x1b[38;5;71m' + desc, ncols=80, colour='#327fba')
    args = {}
    if hasattr(model, 'pipe'): # recurse
        model = model.pipe
    signature = inspect.signature(type(model).__call__, follow_wrapped=True)
    possible = list(signature.parameters)
    debug(f'Diffusers pipeline possible: {possible}')
    prompts, negative_prompts, prompts_2, negative_prompts_2 = fix_prompts(prompts, negative_prompts, prompts_2, negative_prompts_2)
    parser = 'Fixed attention'
    steps = kwargs.get("num_inference_steps", None) or len(getattr(p, 'timesteps', ['1']))
    clip_skip = kwargs.pop("clip_skip", 1)

    # prompt_parser_diffusers.fix_position_ids(model)
    if shared.opts.prompt_attention != 'Fixed attention' and 'Onnx' not in model.__class__.__name__ and (
        'StableDiffusion' in model.__class__.__name__ or
        'StableCascade' in model.__class__.__name__ or
        'Flux' in model.__class__.__name__
    ):
        try:
            prompt_parser_diffusers.encode_prompts(model, p, prompts, negative_prompts, steps=steps, clip_skip=clip_skip)
            parser = shared.opts.prompt_attention
        except Exception as e:
            shared.log.error(f'Prompt parser encode: {e}')
            if os.environ.get('SD_PROMPT_DEBUG', None) is not None:
                errors.display(e, 'Prompt parser encode')
        timer.process.record('encode', reset=False)

    if 'prompt' in possible:
        if 'OmniGen' in model.__class__.__name__:
            prompts = [p.replace('|image|', '<|image_1|>') for p in prompts]
        if hasattr(model, 'text_encoder') and 'prompt_embeds' in possible and len(p.prompt_embeds) > 0 and p.prompt_embeds[0] is not None:
            args['prompt_embeds'] = p.prompt_embeds[0]
            if 'StableCascade' in model.__class__.__name__ and len(getattr(p, 'negative_pooleds', [])) > 0:
                args['prompt_embeds_pooled'] = p.positive_pooleds[0].unsqueeze(0)
            elif 'XL' in model.__class__.__name__ and len(getattr(p, 'positive_pooleds', [])) > 0:
                args['pooled_prompt_embeds'] = p.positive_pooleds[0]
            elif 'StableDiffusion3' in model.__class__.__name__ and len(getattr(p, 'positive_pooleds', [])) > 0:
                args['pooled_prompt_embeds'] = p.positive_pooleds[0]
            elif 'Flux' in model.__class__.__name__ and len(getattr(p, 'positive_pooleds', [])) > 0:
                args['pooled_prompt_embeds'] = p.positive_pooleds[0]
        else:
            args['prompt'] = prompts
    if 'negative_prompt' in possible:
        if hasattr(model, 'text_encoder') and 'negative_prompt_embeds' in possible and len(p.negative_embeds) > 0 and p.negative_embeds[0] is not None:
            args['negative_prompt_embeds'] = p.negative_embeds[0]
            if 'StableCascade' in model.__class__.__name__ and len(getattr(p, 'negative_pooleds', [])) > 0:
                args['negative_prompt_embeds_pooled'] = p.negative_pooleds[0].unsqueeze(0)
            if 'XL' in model.__class__.__name__ and len(getattr(p, 'negative_pooleds', [])) > 0:
                args['negative_pooled_prompt_embeds'] = p.negative_pooleds[0]
            if 'StableDiffusion3' in model.__class__.__name__ and len(getattr(p, 'negative_pooleds', [])) > 0:
                args['negative_pooled_prompt_embeds'] = p.negative_pooleds[0]
        else:
            if 'PixArtSigmaPipeline' in model.__class__.__name__: # pixart-sigma pipeline throws list-of-list for negative prompt
                args['negative_prompt'] = negative_prompts[0]
            else:
                args['negative_prompt'] = negative_prompts

    if 'clip_skip' in possible and parser == 'Fixed attention':
        if clip_skip == 1:
            pass # clip_skip = None
        else:
            args['clip_skip'] = clip_skip - 1

    if 'timesteps' in possible:
        timesteps = re.split(',| ', shared.opts.schedulers_timesteps)
        timesteps = [int(x) for x in timesteps if x.isdigit()]
        if len(timesteps) > 0:
            if hasattr(model.scheduler, 'set_timesteps') and "timesteps" in set(inspect.signature(model.scheduler.set_timesteps).parameters.keys()):
                try:
                    args['timesteps'] = timesteps
                    p.steps = len(timesteps)
                    p.timesteps = timesteps
                    steps = p.steps
                    shared.log.debug(f'Sampler: steps={len(timesteps)} timesteps={timesteps}')
                except Exception as e:
                    shared.log.error(f'Sampler timesteps: {e}')
            else:
                shared.log.warning(f'Sampler: sampler={model.scheduler.__class__.__name__} timesteps not supported')

    if hasattr(model, 'scheduler') and hasattr(model.scheduler, 'noise_sampler_seed') and hasattr(model.scheduler, 'noise_sampler'):
        model.scheduler.noise_sampler = None # noise needs to be reset instead of using cached values
        model.scheduler.noise_sampler_seed = p.seeds # some schedulers have internal noise generator and do not use pipeline generator
    if 'noise_sampler_seed' in possible:
        args['noise_sampler_seed'] = p.seeds
    if 'guidance_scale' in possible:
        args['guidance_scale'] = p.cfg_scale
    if 'img_guidance_scale' in possible and hasattr(p, 'image_cfg_scale'):
        args['img_guidance_scale'] = p.image_cfg_scale
    if 'generator' in possible:
        args['generator'] = get_generator(p)
    if 'latents' in possible and getattr(p, "init_latent", None) is not None:
        if sd_models.get_diffusers_task(model) == sd_models.DiffusersTaskType.TEXT_2_IMAGE:
            args['latents'] = p.init_latent
    if 'output_type' in possible:
        if not hasattr(model, 'vae'):
            args['output_type'] = 'np' # only set latent if model has vae

    # stable cascade
    if 'StableCascade' in model.__class__.__name__:
        kwargs.pop("guidance_scale") # remove
        kwargs.pop("num_inference_steps") # remove
        if 'prior_num_inference_steps' in possible:
            args["prior_num_inference_steps"] = p.steps
            args["num_inference_steps"] = p.refiner_steps
        if 'prior_guidance_scale' in possible:
            args["prior_guidance_scale"] = p.cfg_scale
        if 'decoder_guidance_scale' in possible:
            args["decoder_guidance_scale"] = p.image_cfg_scale

    # set callbacks
    if 'prior_callback_steps' in possible:  # Wuerstchen / Cascade
        args['prior_callback_steps'] = 1
    elif 'callback_steps' in possible:
        args['callback_steps'] = 1

    set_callbacks_p(p)
    if 'prior_callback_on_step_end' in possible: # Wuerstchen / Cascade
        args['prior_callback_on_step_end'] = diffusers_callback
        if 'prior_callback_on_step_end_tensor_inputs' in possible:
            args['prior_callback_on_step_end_tensor_inputs'] = ['latents']
    elif 'callback_on_step_end' in possible:
        args['callback_on_step_end'] = diffusers_callback
        if 'callback_on_step_end_tensor_inputs' in possible:
            if 'prompt_embeds' in possible and 'negative_prompt_embeds' in possible and hasattr(model, '_callback_tensor_inputs'):
                args['callback_on_step_end_tensor_inputs'] = model._callback_tensor_inputs # pylint: disable=protected-access
            else:
                args['callback_on_step_end_tensor_inputs'] = ['latents']
    elif 'callback' in possible:
        args['callback'] = diffusers_callback_legacy

    # handle remaining args
    for arg in kwargs:
        if arg in possible: # add kwargs
            args[arg] = kwargs[arg]
        else:
            pass

    task_kwargs = task_specific_kwargs(p, model)
    for arg in task_kwargs:
        # if arg in possible and arg not in args: # task specific args should not override args
        if arg in possible:
            args[arg] = task_kwargs[arg]
    task_args = getattr(p, 'task_args', {})
    debug(f'Diffusers task args: {task_args}')
    for k, v in task_args.items():
        if k in possible:
            args[k] = v
        else:
            debug(f'Diffusers unknown task args: {k}={v}')
    cross_attention_args = getattr(p, 'cross_attention_kwargs', {})
    debug(f'Diffusers cross-attention args: {cross_attention_args}')
    for k, v in cross_attention_args.items():
        if args.get('cross_attention_kwargs', None) is None:
            args['cross_attention_kwargs'] = {}
        args['cross_attention_kwargs'][k] = v

    # handle missing resolution
    if args.get('image', None) is not None and ('width' not in args or 'height' not in args):
        if 'width' in possible and 'height' in possible:
            if isinstance(args['image'], torch.Tensor) or isinstance(args['image'], np.ndarray):
                args['width'] = 8 * args['image'].shape[-1]
                args['height'] = 8 * args['image'].shape[-2]
            else:
                args['width'] = 8 * math.ceil(args['image'][0].width / 8)
                args['height'] = 8 * math.ceil(args['image'][0].height / 8)

    # handle implicit controlnet
    if 'control_image' in possible and 'control_image' not in args and 'image' in args:
        debug('Diffusers: set control image')
        args['control_image'] = args['image']

    sd_hijack_hypertile.hypertile_set(p, hr=len(getattr(p, 'init_images', [])) > 0)

    # debug info
    clean = args.copy()
    clean.pop('cross_attention_kwargs', None)
    clean.pop('callback', None)
    clean.pop('callback_steps', None)
    clean.pop('callback_on_step_end', None)
    clean.pop('callback_on_step_end_tensor_inputs', None)
    if 'prompt' in clean:
        clean['prompt'] = len(clean['prompt'])
    if 'negative_prompt' in clean:
        clean['negative_prompt'] = len(clean['negative_prompt'])
    clean.pop('generator', None)
    clean['parser'] = parser
    for k, v in clean.items():
        if isinstance(v, torch.Tensor) or isinstance(v, np.ndarray):
            clean[k] = v.shape
        if isinstance(v, list) and len(v) > 0 and (isinstance(v[0], torch.Tensor) or isinstance(v[0], np.ndarray)):
            clean[k] = [x.shape for x in v]
    shared.log.debug(f'Diffuser pipeline: {model.__class__.__name__} task={sd_models.get_diffusers_task(model)} batch={p.iteration + 1}/{p.n_iter}x{p.batch_size} set={clean}')

    if p.hdr_clamp or p.hdr_maximize or p.hdr_brightness != 0 or p.hdr_color != 0 or p.hdr_sharpen != 0:
        txt = 'HDR:'
        txt += f' Brightness={p.hdr_brightness}' if p.hdr_brightness != 0 else ' Brightness off'
        txt += f' Color={p.hdr_color}' if p.hdr_color != 0 else ' Color off'
        txt += f' Sharpen={p.hdr_sharpen}' if p.hdr_sharpen != 0 else ' Sharpen off'
        txt += f' Clamp threshold={p.hdr_threshold} boundary={p.hdr_boundary}' if p.hdr_clamp else ' Clamp off'
        txt += f' Maximize boundary={p.hdr_max_boundry} center={p.hdr_max_center}' if p.hdr_maximize else ' Maximize off'
        shared.log.debug(txt)
    if shared.cmd_opts.profile:
        t1 = time.time()
        shared.log.debug(f'Profile: pipeline args: {t1-t0:.2f}')
    debug(f'Diffusers pipeline args: {args}')
    return args
