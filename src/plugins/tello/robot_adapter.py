import os
import asyncio

from djitellopy import Tello


class TelloAdapter:
    """Wrapper around djitellopy.Tello for use in async FastMCP tools.

    The djitellopy library is synchronous (UDP sockets + blocking waits).
    This wrapper runs all Tello commands in a thread pool executor so they
    don't block the FastMCP async event loop.
    """

    def __init__(self):
        host = os.getenv("TELLO_HOST", Tello.TELLO_IP)
        self.tello = Tello(host=host)
        self._connected = False

    async def connect(self) -> dict:
        """Enter SDK mode and establish communication."""
        await asyncio.to_thread(self.tello.connect)
        self._connected = True
        return {"status": "connected"}

    async def takeoff(self) -> dict:
        """Take off and hover."""
        await self._ensure_connected()
        await asyncio.to_thread(self.tello.takeoff)
        return {"status": "ok", "action": "takeoff"}

    async def land(self) -> dict:
        """Land the drone."""
        await self._ensure_connected()
        await asyncio.to_thread(self.tello.land)
        return {"status": "ok", "action": "land"}

    async def move(self, direction: str, distance: int) -> dict:
        """Move in a direction by distance cm (20-500)."""
        await self._ensure_connected()
        move_fn = getattr(self.tello, f"move_{direction}")
        await asyncio.to_thread(move_fn, distance)
        return {"status": "ok", "direction": direction, "distance_cm": distance}

    async def rotate(self, direction: str, degrees: int) -> dict:
        """Rotate clockwise or counter_clockwise by degrees (1-360)."""
        await self._ensure_connected()
        if direction == "clockwise":
            await asyncio.to_thread(self.tello.rotate_clockwise, degrees)
        else:
            await asyncio.to_thread(self.tello.rotate_counter_clockwise, degrees)
        return {"status": "ok", "direction": direction, "degrees": degrees}

    async def flip(self, direction: str) -> dict:
        """Flip in a direction: forward, back, left, right."""
        await self._ensure_connected()
        flip_fn = getattr(self.tello, f"flip_{direction}")
        await asyncio.to_thread(flip_fn)
        return {"status": "ok", "action": "flip", "direction": direction}

    async def get_status(self) -> dict:
        """Get operational status."""
        await self._ensure_connected()
        return await asyncio.to_thread(self._read_status)

    async def get_attitude(self) -> dict:
        """Get IMU attitude and spatial data."""
        await self._ensure_connected()
        return await asyncio.to_thread(self._read_attitude)

    async def get_drone_info(self) -> dict:
        """Get static drone info."""
        await self._ensure_connected()
        return await asyncio.to_thread(self._read_info)

    async def is_online(self) -> dict:
        """Check if the drone is reachable."""
        try:
            if not self._connected:
                await self.connect()
            battery = await asyncio.to_thread(self.tello.get_battery)
            return {"online": True, "battery": battery}
        except Exception:
            return {"online": False}

    async def disconnect(self):
        """Clean up the Tello connection."""
        try:
            await asyncio.to_thread(self.tello.end)
        except Exception:
            pass
        self._connected = False

    async def _ensure_connected(self):
        """Auto-connect if not already connected."""
        if not self._connected:
            await self.connect()

    def _read_status(self) -> dict:
        """Synchronous helper to read status fields."""
        return {
            "battery": self.tello.get_battery(),
            "height_cm": self.tello.get_height(),
            "flight_time_s": self.tello.get_flight_time(),
            "temperature_c": self.tello.get_temperature(),
            "is_flying": self.tello.is_flying,
        }

    def _read_attitude(self) -> dict:
        """Synchronous helper to read attitude fields."""
        return {
            "pitch": self.tello.get_pitch(),
            "roll": self.tello.get_roll(),
            "yaw": self.tello.get_yaw(),
            "barometer_cm": self.tello.get_barometer(),
            "tof_distance_cm": self.tello.get_distance_tof(),
            "speed_x": self.tello.get_speed_x(),
            "speed_y": self.tello.get_speed_y(),
            "speed_z": self.tello.get_speed_z(),
        }

    def _read_info(self) -> dict:
        """Synchronous helper to read static info."""
        return {
            "sdk_version": self.tello.query_sdk_version(),
            "serial_number": self.tello.query_serial_number(),
            "wifi_snr": self.tello.query_wifi_signal_noise_ratio(),
        }
