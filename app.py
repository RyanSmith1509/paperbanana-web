import os
import uuid
import asyncio
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="PaperBanana Web", version="1.0.0")

OUTPUT_DIR = Path("generated_outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


class GenerateRequest(BaseModel):
    description: str
    caption: str = "Generated figure"
    figure_type: str = "diagram"


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    png_url: str | None = None
    svg_url: str | None = None
    message: str = ""


def get_env_with_api_key():
    env = os.environ.copy()
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set.")
    env["GOOGLE_API_KEY"] = api_key
    return env


def write_env_file(directory):
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if api_key:
        env_path = Path(directory) / ".env"
        env_path.write_text(f"GOOGLE_API_KEY={api_key}\n")


def convert_png_to_svg(png_path, svg_path):
    try:
        subprocess.run(["convert", str(png_path), str(svg_path)], check=True, capture_output=True, timeout=30)
    except (subprocess.CalledProcessError, FileNotFoundError):
        import base64
        import struct
        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        with open(png_path, "rb") as f:
            f.read(16)
            width, height = struct.unpack(">II", f.read(8))
        svg_content = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            + f'width="{width}" height="{height}" '
            + f'viewBox="0 0 {width} {height}">'
            + f'<image width="{width}" height="{height}" '
            + f'xlink:href="data:image/png;base64,{b64}"/>'
            + '</svg>'
        )
        svg_path.write_text(svg_content)


@app.get("/api/health")
async def health_check():
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    has_key = bool(api_key) and len(api_key) > 10
    pb_check = shutil.which("paperbanana")
    return {
        "status": "ok",
        "api_key_configured": has_key,
        "api_key_preview": f"{api_key[:8]}..." if has_key else "NOT SET",
        "paperbanana_found": pb_check is not None,
        "paperbanana_path": pb_check,
    }


@app.post("/api/generate", response_model=GenerateResponse)
async def generate_figure(request: GenerateRequest):
    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_file = job_dir / "input.txt"
    input_file.write_text(request.description)
    write_env_file(job_dir)
    env = get_env_with_api_key()
    try:
        if request.figure_type == "plot":
            cmd = ["paperbanana", "plot", "--input", str(input_file.resolve()), "--caption", request.caption]
        else:
            cmd = ["paperbanana", "generate", "--input", str(input_file.resolve()), "--caption", request.caption]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(job_dir) env=env)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            combined = f"STDOUT: {stdout_text[:300]} | STDERR: {stderr_text[:300]} | EXIT CODE: {process.returncode}"
            raise HTTPException(status_code=500, detail=f"PaperBanana failed: {combined}")
        png_file = None
        for p in job_dir.rglob("final_output.png"):
            png_file = p
            break
        if png_file is None:
            for p in job_dir.rglob("*.png"):
                png_file = p
                break
        if png_file is None:
            files_found = list(job_dir.rglob("*"))
            raise HTTPException(status_code=500, detail=f"No image generated. Files in output: {[str(f.name) for f in files_found[:20]]} | STDOUT: {stdout_text[:200]}")
        final_png = job_dir / "figure.png"
        shutil.copy2(png_file, final_png)
        final_svg = job_dir / "figure.svg"
        convert_png_to_svg(final_png, final_svg)
        return GenerateResponse(
            job_id=job_id,
            status="success",
            png_url=f"/outputs/{job_id}/figure.png",
            svg_url=f"/outputs/{job_id}/figure.svg",
            message="Figure generated successfully!",
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Generation timed out. Try a simpler description.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)[:500]}")


@app.get("/api/download/{job_id}/{format}")
async def download_file(job_id: str, format: str):
    if format not in ("png", "svg"):
        raise HTTPException(status_code=400, detail="Format must be png or svg")
    file_path = OUTPUT_DIR / job_id / f"figure.{format}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "image/png" if format == "png" else "image/svg+xml"
    return FileResponse(path=str(file_path), media_type=media_type, filename=f"paperbanana_figure.{format}")


@app.get("/", response_class=HTMLResponse)
async def home():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text())
