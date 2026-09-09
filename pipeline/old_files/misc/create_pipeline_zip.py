import zipfile
from pathlib import Path

PIPELINE_DIR = Path(r"c:\Users\jaspi\OneDrive\Desktop\dependabot-failing\Dependascope\pipeline")
ZIP_OUT = Path(r"c:\Users\jaspi\OneDrive\Desktop\dependabot-failing\pipeline.zip")

print(f"Creating zip file: {ZIP_OUT}")

with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for file in PIPELINE_DIR.iterdir():
        if file.is_file() and file.suffix in [".py", ".md", ".txt"]:
            arcname = Path("pipeline") / file.name
            zf.write(file, arcname)
            print(f"  Added: {file.name}")

print(f"Zip created successfully at {ZIP_OUT} (Size: {ZIP_OUT.stat().st_size / 1024:.1f} KB)")
