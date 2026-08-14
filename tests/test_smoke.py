def test_import():
    import tradingagent  # noqa: F401


def test_cli_help_lists_stage_flag():
    from typer.testing import CliRunner

    from tradingagent.__main__ import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--stage" in result.output
