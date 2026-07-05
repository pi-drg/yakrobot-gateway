"""``yakrobot-py`` — management CLI for the yakrobot-gateway MCP server.

Replaces the ``uv run python scripts/...`` invocations with a single entrypoint:

    yakrobot-py serve --robots fakerobot --tunnel ngrok
    yakrobot-py robots
    yakrobot-py status
    yakrobot-py export tumbller          # public domain defaults from $NGROK_DOMAIN
    yakrobot-py sim

Built with Typer. The command logic lives in ``commands.py`` (shared with the legacy
``scripts/`` entrypoints); this module is only the argument surface.
"""

from enum import Enum
from typing import Optional

import typer
from dotenv import load_dotenv

from . import commands

# Load .env so env-var defaults below (e.g. NGROK_DOMAIN) resolve from the repo's .env,
# matching how core.server loads it for the serving path.
load_dotenv()

app = typer.Typer(
    help="Manage the yakrobot-gateway MCP server",
    no_args_is_help=True,
    add_completion=True,
)


class TunnelProvider(str, Enum):
    ngrok = "ngrok"
    cloudflare = "cloudflare"


@app.command()
def serve(
    robots: Optional[list[str]] = typer.Option(
        None, "--robots", help="Robot plugin(s) to load; repeat for many (default: all)"
    ),
    port: int = typer.Option(8000, help="Port to bind"),
    tunnel: Optional[TunnelProvider] = typer.Option(
        None, help="Expose the gateway over a public tunnel"
    ),
):
    """Run the gateway MCP server (blocking)."""
    commands.serve_gateway(robots or None, port, tunnel.value if tunnel else None)


@app.command()
def robots():
    """List available robot plugins."""
    commands.list_robots()


@app.command()
def status(
    url: str = typer.Option("http://localhost:8000", help="Gateway base URL"),
):
    """Show a running gateway's mounted robots + reservations."""
    commands.gateway_status(url)


@app.command()
def export(
    robot: str = typer.Argument(..., help="Robot plugin name (e.g. tumbller)"),
    public_domain: str = typer.Option(
        "",
        "--public-domain",
        envvar=["NGROK_DOMAIN", "CLOUDFLARE_DOMAIN"],
        help="Public domain to resolve MCP/fleet endpoints (any tunnel). "
        "Defaults to $NGROK_DOMAIN, then $CLOUDFLARE_DOMAIN.",
    ),
    out: Optional[str] = typer.Option(
        None, help="Output path (default: robot-descriptors/<robot>.json)"
    ),
    stdout: bool = typer.Option(False, "--stdout", help="Print to stdout instead of writing a file"),
):
    """Export a robot's JSON descriptor."""
    commands.export_descriptor(robot, public_domain, out, stdout)


@app.command()
def sim(port: int = typer.Option(8080, help="Port to bind")):
    """Start the hardware-free fakerobot simulator."""
    commands.run_simulator(port)


def main(argv=None) -> None:
    """Console-script entrypoint. ``argv`` lets the legacy scripts/ shims forward args."""
    app(args=argv)
