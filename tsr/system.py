import math
import os
from dataclasses import dataclass, field
from typing import List, Union
import logging


import numpy as np
import PIL.Image
import torch
import torch.nn.functional as F
import trimesh
from einops import rearrange
from huggingface_hub import hf_hub_download
from omegaconf import OmegaConf
from PIL import Image

from .models.isosurface import MarchingCubeHelper
from .utils import (
    BaseModule,
    ImagePreprocessor,
    find_class,
    get_spherical_cameras,
    scale_tensor,
)


class TSR(BaseModule):
    @dataclass
    class Config(BaseModule.Config):
        cond_image_size: int

        image_tokenizer_cls: str
        image_tokenizer: dict

        tokenizer_cls: str
        tokenizer: dict

        backbone_cls: str
        backbone: dict

        post_processor_cls: str
        post_processor: dict

        decoder_cls: str
        decoder: dict

        renderer_cls: str
        renderer: dict

    cfg: Config
    
    @classmethod
    def from_pretrained(
            cls, weight_path: str, config_path: str
    ):
        cfg = OmegaConf.load(config_path)
        OmegaConf.resolve(cfg)
        model = cls(cfg)
        ckpt = torch.load(weight_path, map_location="cpu")
        model.load_state_dict(ckpt)
        return model

    def configure(self):
        self.image_tokenizer = find_class(self.cfg.image_tokenizer_cls)(
            self.cfg.image_tokenizer
        )
        self.tokenizer = find_class(self.cfg.tokenizer_cls)(self.cfg.tokenizer)
        self.backbone = find_class(self.cfg.backbone_cls)(self.cfg.backbone)
        self.post_processor = find_class(self.cfg.post_processor_cls)(
            self.cfg.post_processor
        )
        self.decoder = find_class(self.cfg.decoder_cls)(self.cfg.decoder)
        self.renderer = find_class(self.cfg.renderer_cls)(self.cfg.renderer)
        self.image_processor = ImagePreprocessor()
        self.isosurface_helper = None

    def forward(
        self,
        image: Union[
            PIL.Image.Image,
            np.ndarray,
            torch.FloatTensor,
            List[PIL.Image.Image],
            List[np.ndarray],
            List[torch.FloatTensor],
        ],
        device: str,
    ) -> torch.FloatTensor:
        rgb_cond = self.image_processor(image, self.cfg.cond_image_size)[:, None].to(
            device
        )
        batch_size = rgb_cond.shape[0]

        input_image_tokens: torch.Tensor = self.image_tokenizer(
            rearrange(rgb_cond, "B Nv H W C -> B Nv C H W", Nv=1),
        )

        input_image_tokens = rearrange(
            input_image_tokens, "B Nv C Nt -> B (Nv Nt) C", Nv=1
        )

        tokens: torch.Tensor = self.tokenizer(batch_size)

        tokens = self.backbone(
            tokens,
            encoder_hidden_states=input_image_tokens,
        )

        scene_codes = self.post_processor(self.tokenizer.detokenize(tokens))
        return scene_codes

    def render(
        self,
        scene_codes,
        n_views: int,
        elevation_deg: float = 0.0,
        camera_distance: float = 1.9,
        fovy_deg: float = 40.0,
        height: int = 256,
        width: int = 256,
        return_type: str = "pil",
    ):
        rays_o, rays_d = get_spherical_cameras(
            n_views, elevation_deg, camera_distance, fovy_deg, height, width
        )
        rays_o, rays_d = rays_o.to(scene_codes.device), rays_d.to(scene_codes.device)

        def process_output(image: torch.FloatTensor):
            if return_type == "pt":
                return image
            elif return_type == "np":
                return image.detach().cpu().numpy()
            elif return_type == "pil":
                return Image.fromarray(
                    (image.detach().cpu().numpy() * 255.0).astype(np.uint8)
                )
            else:
                raise NotImplementedError

        images = []
        for scene_code in scene_codes:
            images_ = []
            for i in range(n_views):
                with torch.no_grad():
                    image = self.renderer(
                        self.decoder, scene_code, rays_o[i], rays_d[i]
                    )
                images_.append(process_output(image))
            images.append(images_)

        return images

    def set_marching_cubes_resolution(self, resolution: int):
        logging.info('Setting marching cubes resolution...')
        try:
            if (
                self.isosurface_helper is not None
                and self.isosurface_helper.resolution == resolution
            ):
                logging.info('Marching cubes resolution is already set to the desired value.')
                return
            self.isosurface_helper = MarchingCubeHelper(resolution)
            logging.info('Marching cubes resolution set successfully.')
        except Exception as e:
            logging.error('Failed to set marching cubes resolution: %s', e)

    def extract_mesh(self, scene_codes, resolution: int = 256, threshold: float = 25.0):
        logging.info('Starting mesh extraction...')
        try:
            self.set_marching_cubes_resolution(resolution)
        except Exception as e:
            logging.error('Failed to set marching cubes resolution: %s', e)
            return None

        meshes = []
        for scene_code in scene_codes:
            logging.info('Processing scene code...')
            try:
                with torch.no_grad():
                    logging.info('Querying triplane for density...')
                    try:
                        density = self.renderer.query_triplane(
                            self.decoder,
                            scale_tensor(
                                self.isosurface_helper.grid_vertices.to(scene_codes.device),
                                self.isosurface_helper.points_range,
                                (-self.renderer.cfg.radius, self.renderer.cfg.radius),
                            ),
                            scene_code,
                        )["density_act"]
                    except Exception as e:
                        logging.error('Failed to query triplane for density: %s', e)
                        continue
                    logging.info('Successfully queried triplane for density.')
            except Exception as e:
                logging.error('Failed to process scene code: %s', e)
                continue
            logging.info('Scene code processed successfully.')

            try:
                logging.info('Calculating v_pos and t_pos_idx...')
                v_pos, t_pos_idx = self.isosurface_helper(-(density - threshold))
                v_pos = scale_tensor(
                    v_pos,
                    self.isosurface_helper.points_range,
                    (-self.renderer.cfg.radius, self.renderer.cfg.radius),
                )
                logging.info('Successfully calculated v_pos and t_pos_idx.')
            except Exception as e:
                logging.error('Failed to calculate v_pos and t_pos_idx: %s', e)
                continue

            try:
                logging.info('Querying triplane for color...')
                with torch.no_grad():
                    color = self.renderer.query_triplane(
                        self.decoder,
                        v_pos,
                        scene_code,
                    )["color"]
                logging.info('Successfully queried triplane for color.')
            except Exception as e:
                logging.error('Failed to query triplane for color: %s', e)
                continue

            try:
                logging.info('Creating and appending mesh...')
                mesh = trimesh.Trimesh(
                    vertices=v_pos.cpu().numpy(),
                    faces=t_pos_idx.cpu().numpy(),
                    vertex_colors=color.cpu().numpy(),
                )
                meshes.append(mesh)
                logging.info('Successfully created and appended mesh.')
            except Exception as e:
                logging.error('Failed to create and append mesh: %s', e)
                continue

            logging.info('Mesh extraction completed.')
            return meshes