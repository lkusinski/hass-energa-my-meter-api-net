"""Tests for Energa automation blueprints."""

import glob
import os


def test_blueprints_files_exist_and_valid():
    bp_files = glob.glob("custom_components/energa_mobile/blueprints/automation/energa_mobile/*.yaml")
    assert len(bp_files) >= 3, f"Expected at least 3 blueprints, found {len(bp_files)}"

    for bp_path in bp_files:
        assert os.path.exists(bp_path)
        with open(bp_path, encoding="utf-8") as f:
            content = f.read()

        assert "blueprint:" in content, f"{bp_path} missing 'blueprint:'"
        assert "name:" in content, f"{bp_path} missing 'name:'"
        assert "domain: automation" in content, f"{bp_path} missing 'domain: automation'"
        assert "trigger:" in content, f"{bp_path} missing 'trigger:'"
        assert "action:" in content, f"{bp_path} missing 'action:'"
