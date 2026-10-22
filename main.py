from pathlib import Path
import os
import shutil

from config import ROOT, load_config, resolve_path


def main() -> None:
    settings = load_config()
    os.chdir(ROOT)
    # Load/validate agent data before any cleanup or mask writes.
    from editor_model import Draft
    Draft(ROOT).validate()
    from agent_data import gen_textures, create_ini, export
    from masks import gen_masks

    export_dir: Path = resolve_path(ROOT, settings["EXPORT_DIR"])
    gen_masks()
    if settings["CLEAN_EXPORTS"]:
        print(f"Force clean requested! Recreating {export_dir}...")
        if export_dir.exists():
            shutil.rmtree(export_dir)
        export_dir.mkdir(parents=True)
    gen_textures()
    create_ini()
    export()


if __name__ == "__main__":
    main()
