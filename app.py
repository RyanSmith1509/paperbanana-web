"""
PaperBanana Web App
A simple web interface for generating academic figures using PaperBanana.
"""

import os
import uuid
import asyncio
import shutil
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import json

app = FastAPI(title="PaperBanana Web", version="1.0.0")

# Directory to store generated outputs
OUTPUT_DIR = Path("generated_outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# Serve the generated files as static files
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


class GenerateRequest(BaseModel):
    description: str
    caption: str = "Generated figure"
    figure_type: str = "diagram"  # "diagram" or "plot"


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    png_url: str | None = None
    svg_url: str | None = None
    message: str = ""


def convert_png_to_svg(png_path: Path, svg_path: Path):
    """
    Convert PNG to SVG using potrace (via bitmap tracing).
    Falls back to embedding the PNG in an SVG wrapper if potrace isn't available.
    """
    try:
        # Try using cairosvg or potrace if available
        subprocess.run(
            ["convert", str(png_path), str(svg_path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: wrap PNG inside an SVG file
        import base64

        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        # Read image dimensions (basic PNG header parsing)
        with open(png_path, "rb") as f:
            f.read(16)
            import struct
            width, height = struct.unpack(">II", f.read(8))

        svg_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="{width}" height="{height}"
     viewBox="0 0 {width} {height}">
  <image width="{width}" height="{height}"
         xlink:href="data:image/png;base64,{b64}"/>
</svg>'''
        svg_path.write_text(svg_content)


@app.post("/api/generate", response_model=GenerateResponse)
async def generate_figure(request: GenerateRequest):
    """Generate a figure from a text description."""

    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Write the description to a temp file
    input_file = job_dir / "input.txt"
    input_file.write_text(request.description)

    try:
        # Determine which PaperBanana command to run
        if request.figure_type == "plot":
            cmd = [
                "paperbanana", "plot",
                "--input", str(input_file),
                "--caption", request.caption,
            ]
        else:
            cmd = [
                "paperbanana", "generate",
                "--input", str(input_file),
                "--caption", request.caption,
            ]

        # Run PaperBanana as a subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(job_dir),
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=300,  # 5 minute timeout
        )

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=500,
                detail=f"PaperBanana failed: {error_msg[:500]}",
            )

        # Find the generated PNG output
        # PaperBanana outputs to outputs/run_<timestamp>/final_output.png
        png_file = None
        for p in job_dir.rglob("final_output.png"):
            png_file = p
            break

        # Also check for any PNG if final_output.png isn't found
        if png_file is None:
            for p in job_dir.rglob("*.png"):
                png_file = p
                break

        if png_file is None:
            raise HTTPException(
                status_code=500,
                detail="No output image was generated. Check your description and try again.",
            )

        # Copy PNG to a clean location
        final_png = job_dir / "figure.png"
        shutil.copy2(png_file, final_png)

        # Generate SVG version
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
        raise HTTPException(
            status_code=504,
            detail="Generation timed out after 5 minutes. Try a simpler description.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)[:500]}",
        )


@app.get("/api/download/{job_id}/{format}")
async def download_file(job_id: str, format: str):
    """Download a generated figure in the specified format."""

    if format not in ("png", "svg"):
        raise HTTPException(status_code=400, detail="Format must be 'png' or 'svg'")

    file_path = OUTPUT_DIR / job_id / f"figure.{format}"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "image/png" if format == "png" else "image/svg+xml"
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=f"paperbanana_figure.{format}",
    )


@app.get("/", response_class=HTMLResponse)
async def home():
    """Serve the main page."""
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text())
