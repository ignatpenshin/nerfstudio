# Copyright 2022 The Plenoptix Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Code to train model.
"""
import logging
import os
import random
from typing import Dict

import torch
import torch.distributed as dist
import umsgpack
from torch.nn.parallel import DistributedDataParallel as DDP
from torchtyping import TensorType

from pyrad.cameras.cameras import get_camera, get_intrinsics_from_intrinsics_matrix
from pyrad.cameras.rays import RayBundle
from pyrad.data.dataloader import EvalDataloader, setup_dataset_eval, setup_dataset_train
from pyrad.graphs.base import setup_graph
from pyrad.optimizers.optimizers import setup_optimizers
from pyrad.utils import profiler, writer
from pyrad.utils.config import Config
from pyrad.utils.decorators import check_main_thread
from pyrad.utils.writer import EventName, TimeWriter
from pyrad.viewer.backend import vis_utils
from pyrad.viewer.backend.utils import get_intrinsics_matrix_and_camera_to_world_h
from pyrad.viewer.backend.visualizer import ViewerWindow, Visualizer

logging.getLogger("PIL").setLevel(logging.WARNING)


class Trainer:
    """Training class

    Args:
        config (Config): The configuration object.
        local_rank (int, optional): Local rank of the process. Defaults to 0.
        world_size (int, optional): World size of the process. Defaults to 1.
    """

    def __init__(self, config: Config, local_rank: int = 0, world_size: int = 1):
        self.config = config
        self.local_rank = local_rank
        self.world_size = world_size
        self.device = "cpu" if world_size == 0 else f"cuda:{local_rank}"
        # dataset variables
        self.dataset_inputs_train = None
        self.dataloader_train = None
        self.dataloader_eval = None
        # model variables
        self.graph = None
        self.optimizers = None
        self.start_step = 0
        # logging variables
        writer.setup_event_writers(config.logging, max_iter=config.trainer.max_num_iterations)
        profiler.setup_profiler(config.logging)

        self.vis = None
        if self.config.viewer.enable:
            window = ViewerWindow(zmq_url=self.config.viewer.zmq_url)
            self.vis = Visualizer(window=window)
            logging.info("Connected to viewer at %s", self.config.viewer.zmq_url)
            self.vis.delete()
        else:
            logging.info("Continuing without viewer.")

    def setup(self, test_mode=False):
        """Setup the Trainer by calling other setup functions."""
        self.dataset_inputs_train, self.dataloader_train = setup_dataset_train(self.config.data, device=self.device)
        _, self.dataloader_eval = setup_dataset_eval(self.config.data, test_mode=test_mode, device=self.device)
        self.graph = setup_graph(self.config.graph, self.dataset_inputs_train, device=self.device)
        self.optimizers = setup_optimizers(self.config.optimizers, self.graph.get_param_groups())

        if self.config.trainer.resume_train.load_dir:
            self._load_checkpoint()

        if self.world_size > 1:
            self.graph = DDP(self.graph, device_ids=[self.local_rank])
            dist.barrier(device_ids=[self.local_rank])

        self.graph.register_callbacks()

    @classmethod
    def get_aggregated_loss(cls, loss_dict: Dict[str, torch.tensor]):
        """Returns the aggregated losses and the scalar for calling .backwards() on.
        # TODO: move this out to another file/class/etc.
        """
        loss_sum = 0.0
        for loss_name in loss_dict.keys():
            # TODO(ethan): add loss weightings here from a config
            loss_sum += loss_dict[loss_name]
        return loss_sum

    def train(self) -> None:
        """Train the model."""

        if self.vis:
            self.draw_scene_in_viewer()

        with TimeWriter(writer, EventName.TOTAL_TRAIN_TIME):
            num_iterations = self.config.trainer.max_num_iterations
            iter_dataloader_train = iter(self.dataloader_train)
            for step in range(self.start_step, self.start_step + num_iterations):
                with TimeWriter(writer, EventName.ITER_LOAD_TIME, step=step):
                    ray_indices, batch = next(iter_dataloader_train)

                with TimeWriter(writer, EventName.ITER_TRAIN_TIME, step=step) as t:
                    loss_dict = self.train_iteration(ray_indices, batch, step)
                writer.put_scalar(name=EventName.RAYS_PER_SEC, scalar=ray_indices.shape[0] / t.duration, step=step)

                if step != 0 and step % self.config.logging.steps_per_log == 0:
                    writer.put_dict(name="Loss/train-loss_dict", scalar_dict=loss_dict, step=step)
                if step != 0 and self.config.trainer.steps_per_save and step % self.config.trainer.steps_per_save == 0:
                    self._save_checkpoint(self.config.trainer.model_dir, step)
                if (
                    self.vis
                    and step != 0
                    and self.config.viewer.steps_per_render_image
                    and step % self.config.viewer.steps_per_render_image == 0
                ):
                    _ = self.render_image_in_viewer()
                if step % self.config.trainer.steps_per_test == 0:
                    self.eval_with_dataloader(self.dataloader_eval, step=step)
                self._write_out_storage(step)

        self._write_out_storage(num_iterations)

    def _write_out_storage(self, step: int) -> None:
        """Perform writes only during appropriate time steps

        Args:
            step (int): Current training step.
        """
        if (
            step % self.config.logging.steps_per_log == 0
            or (self.config.trainer.steps_per_save and step % self.config.trainer.steps_per_save == 0)
            or step % self.config.trainer.steps_per_test == 0
            or step == self.config.trainer.max_num_iterations
        ):
            writer.write_out_storage()

    def _load_checkpoint(self) -> None:
        """helper function to load graph and optimizer from prespecified checkpoint"""
        load_config = self.config.trainer.resume_train
        load_path = os.path.join(load_config.load_dir, f"step-{load_config.load_step:09d}.ckpt")
        assert os.path.exists(load_path), f"Checkpoint {load_path} does not exist"
        loaded_state = torch.load(load_path, map_location="cpu")
        self.start_step = loaded_state["step"] + 1
        # load the checkpoints for graph and optimizer
        self.graph.load_graph(loaded_state)
        self.optimizers.load_optimizers(loaded_state)
        logging.info("done loading checkpoint from %s", load_path)

    @check_main_thread
    def _save_checkpoint(self, output_dir: str, step: int) -> None:
        """Save the model and optimizers

        Args:
            output_dir (str): directory to save the checkpoint
            step (int): number of steps in training for given checkpoint
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        ckpt_path = os.path.join(output_dir, f"step-{step:09d}.ckpt")
        torch.save(
            {
                "step": step,
                "model": self.graph.module.state_dict() if hasattr(self.graph, "module") else self.graph.state_dict(),
                "optimizers": {k: v.state_dict() for (k, v) in self.optimizers.optimizers.items()},
            },
            ckpt_path,
        )

    @profiler.time_function
    def train_iteration(self, ray_indices: TensorType["num_rays", 3], batch: dict, step: int) -> Dict[str, float]:
        """Run one iteration with a batch of inputs.

        Args:
            ray_indices (TensorType["num_rays", 3]): Contains camera, row, and col indicies for target rays.
            batch (dict): Batch of training data.
            step (int): Current training step.

        Returns:
            Dict[str, float]: Dictionary of model losses.
        """
        _, loss_dict = self.graph.forward(ray_indices, batch=batch)
        loss = loss_dict["aggregated_loss"]
        self.optimizers.zero_grad_all()
        loss.backward()
        self.optimizers.optimizer_step_all()
        self.optimizers.scheduler_step_all(step)
        if self.graph.callbacks:
            for func_ in self.graph.callbacks:
                func_.after_step(step)
        return loss_dict

    @profiler.time_function
    def test_image(self, camera_ray_bundle: RayBundle, batch: dict, step: int = None) -> float:
        """Test a specific image.

        Args:
            camera_ray_bundle (RayBundle): Bundle of test rays.
            batch (dict): Batch of data.
            step (int): Current training step.

        Returns:
            float: PSNR
        """
        self.graph.eval()
        image_idx = int(camera_ray_bundle.camera_indices[0, 0])
        outputs = self.graph.get_outputs_for_camera_ray_bundle(camera_ray_bundle)
        psnr = self.graph.log_test_image_outputs(image_idx, step, batch, outputs)
        self.graph.train()
        return psnr

    def draw_scene_in_viewer(self):
        """Draw some images and the scene aabb in the viewer."""
        image_dataset_train = self.dataloader_train.image_sampler.dataset
        dataset_inputs = self.dataset_inputs_train
        indices = random.sample(range(len(image_dataset_train)), k=10)
        for idx in indices:
            image = image_dataset_train[idx]["image"]
            camera = get_camera(dataset_inputs.intrinsics[idx], dataset_inputs.camera_to_world[idx], None)
            pose = camera.get_camera_to_world().double().numpy()
            K = camera.get_intrinsics_matrix().double().numpy()
            vis_utils.draw_camera_frustum(
                self.vis,
                image=(image.double().numpy() * 255.0),
                pose=pose,
                K=K,
                height=1.0,
                name=f"image_dataset_train/{idx:06d}",
                displayed_focal_length=0.5,
                realistic=False,
            )
        aabb = dataset_inputs.scene_bounds.aabb
        vis_utils.draw_aabb(self.vis, aabb, name="dataset_inputs_train/scene_bounds/aabb")

    @profiler.time_function
    def render_image_in_viewer(self):
        """
        Draw an image using the current camera pose from the viewer.
        The image is sent of a TCP connection and then uses WebRTC to send it to the viewer.
        """
        data = self.vis["/Cameras/Main Camera"].get_object()
        message = umsgpack.unpackb(data)
        camera_object = message["object"]["object"]
        image_height = self.config.viewer.render_image_height
        intrinsics_matrix, camera_to_world_h = get_intrinsics_matrix_and_camera_to_world_h(
            camera_object, image_height=image_height
        )

        camera_to_world = camera_to_world_h[:3, :]
        intrinsics = get_intrinsics_from_intrinsics_matrix(intrinsics_matrix)
        camera = get_camera(intrinsics, camera_to_world)
        camera_ray_bundle = camera.get_camera_ray_bundle(device=self.device)
        camera_ray_bundle.num_rays_per_chunk = self.config.viewer.num_rays_per_chunk

        self.graph.eval()
        outputs = self.graph.get_outputs_for_camera_ray_bundle(camera_ray_bundle)
        self.graph.train()

        # gross hack to get the image key, depending on which keys the graph uses
        rgb_key = "rgb" if "rgb" in outputs else "rgb_fine"
        # TODO: make it such that the TCP connection doesn't need float64
        image = outputs[rgb_key].cpu().numpy().astype("float64") * 255
        self.vis["/Cameras/Main Camera"].set_image(image)
        return outputs

    def eval_with_dataloader(self, dataloader: EvalDataloader, step: int = None) -> None:
        """Run evaluation with a given dataloader.

        Args:
            dataloader (EvalDataLoader): Evaluation dataloader.
            step (int): Current training iteration.
        """
        for camera_ray_bundle, batch in dataloader:
            self.test_image(camera_ray_bundle, batch, step=step)
