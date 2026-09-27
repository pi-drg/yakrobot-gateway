"""MCP-facing adapter for LeLab (§0.18)."""

from fastmcp import FastMCP

from .robot_adapter import LelabAdapter


def register(mcp: FastMCP, adapter: LelabAdapter) -> None:
    @mcp.tool
    async def lelab_status() -> dict:
        """Merge LeLab's teleoperation, recording and inference status under three keys."""
        teleop = await adapter.get("/teleoperation-status")
        recording = await adapter.get("/recording-status")
        inference = await adapter.get("/inference-status")
        return {"teleoperation": teleop, "recording": recording, "inference": inference}

    @mcp.tool
    async def lelab_joint_positions() -> dict:
        """Return the current leader and follower joint positions."""
        return await adapter.get("/joint-positions")

    @mcp.tool
    async def lelab_list_datasets() -> dict:
        """List the datasets recorded on this LeLab instance."""
        return await adapter.get("/datasets")

    @mcp.tool
    async def lelab_available_cameras() -> dict:
        """List the cameras LeLab can record from."""
        return await adapter.get("/available-cameras")

    @mcp.tool
    async def lelab_start_recording(
        dataset_repo_id: str,
        single_task: str,
        cameras: dict,
        num_episodes: int = 5,
        episode_time_s: int = 30,
        reset_time_s: int = 10,
        fps: int = 30,
    ) -> dict:
        """Start recording episodes into dataset_repo_id.

        cameras is required and must be non-empty: every value has type == "opencv" and
        integer camera_index, width, height and fps, e.g.
        {"front": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}}.
        Call lelab_available_cameras first. A person must move the leader arm; this tool
        only starts the recorder.
        """
        if not cameras:
            raise ValueError(
                "cameras is required: a dataset with no images is useless for imitation learning"
            )
        for name, cam in cameras.items():
            if not isinstance(cam, dict) or cam.get("type") != "opencv":
                raise ValueError(f"camera {name!r} must have type == 'opencv'")
            for field in ("camera_index", "width", "height", "fps"):
                value = cam.get(field)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"camera {name!r} must have an integer {field}")
        rig = await adapter.saved_rig()
        return await adapter.post(
            "/start-recording",
            {
                "dataset_repo_id": dataset_repo_id,
                "single_task": single_task,
                "cameras": cameras,
                "num_episodes": num_episodes,
                "episode_time_s": episode_time_s,
                "reset_time_s": reset_time_s,
                "fps": fps,
                "video": True,
                "push_to_hub": False,
                "leader_port": rig["leader_port"],
                "follower_port": rig["follower_port"],
                "leader_config": rig["leader_config"],
                "follower_config": rig["follower_config"],
            },
        )

    @mcp.tool
    async def lelab_stop_recording() -> dict:
        """Stop the running recording."""
        return await adapter.post("/stop-recording")

    @mcp.tool
    async def lelab_exit_episode_early() -> dict:
        """End the current episode early."""
        return await adapter.post("/recording-exit-early")

    @mcp.tool
    async def lelab_start_inference(policy_ref: str, task: str = "", duration_s: int = 60) -> dict:
        """Start inference with policy_ref on the follower arm."""
        rig = await adapter.saved_rig()
        return await adapter.post(
            "/start-inference",
            {
                "policy_ref": policy_ref,
                "task": task,
                "duration_s": duration_s,
                "follower_port": rig["follower_port"],
                "follower_config": rig["follower_config"],
            },
        )

    @mcp.tool
    async def lelab_stop_inference() -> dict:
        """Stop the running inference."""
        return await adapter.post("/stop-inference")

    @mcp.tool
    async def lelab_upload_dataset(dataset_repo_id: str, private: bool = True) -> dict:
        """Upload dataset_repo_id to the Hugging Face Hub."""
        return await adapter.post("/upload-dataset", {"dataset_repo_id": dataset_repo_id, "private": private})
