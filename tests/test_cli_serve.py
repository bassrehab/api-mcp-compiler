"""What `serve` tells a person to run, read the way a shell will read it.

The line used to print `pip install mcp>=1.2 httpx>=0.27` unquoted. Pasted into a shell, `>`
is a redirection, so it installed `mcp` and `httpx` with no bounds and wrote pip's output to a
file named `=1.2`. Once the requirement gained an upper bound it stopped working at all, since
`<2` asks the shell to read a file called `2`.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from typer.testing import CliRunner

from api_mcp_compiler.cli import app
from api_mcp_compiler.codegen.mcp_server import GENERATED_REQUIREMENTS
from tests.conftest import INVENTORY_SERVICE


def test_the_printed_install_command_survives_a_shell(tmp_path: Path) -> None:
    out = tmp_path / "server with a space.py"
    result = CliRunner().invoke(app, ["serve", INVENTORY_SERVICE, "--out", str(out)])
    assert result.exit_code == 0, result.output

    line = next(item for item in result.output.splitlines() if "run it with:" in item)
    words = shlex.split(line.split("run it with:", 1)[1])

    install = words[: words.index("&&")]
    assert install[:2] == ["pip", "install"]
    assert install[2:] == list(GENERATED_REQUIREMENTS)
    assert words[-2:] == ["python", str(out)]
