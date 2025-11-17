# worker/worker.py
import os
import io
import json
import logging
import re
from datetime import datetime
from typing import Optional, List
import queue
import threading

import dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image, ImageOps
from PIL.Image import Image as PILImage
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openai import OpenAI

from box_sdk_gen import (
    BoxClient,
    BoxJWTAuth,
    FileWithInMemoryCacheTokenStorage,
    JWTConfig,
)
from box_sdk_gen.managers.uploads import (
    UploadFileAttributes,
    UploadFileAttributesParentField,
)

from base64 import b64encode

# -------------------------------------------------
# basic setup
# -------------------------------------------------
dotenv.load_dotenv(".env")  # uses your file baked into the image

logging.basicConfig(level=logging.INFO)
logging.getLogger("box_sdk_gen").setLevel(logging.CRITICAL)

PROMPT_PATH = "./assets/system_prompt.txt"
DOC1_PATH = "./assets/Goodwill-Donation-Value-Guide.txt"
DOC2_PATH = "./assets/Salvation-Army-Donation-Value-Guide.txt"

OPENAI_KEY = os.getenv("OPENAI_KEY")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_KEY not configured in .env")

client = OpenAI(api_key=OPENAI_KEY)

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

app = FastAPI(title="Donation Audit Worker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this in prod if you want
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# in-memory job queue + worker thread
# -------------------------------------------------
job_queue: "queue.Queue[tuple[bytes, str]]" = queue.Queue()


def _queue_worker_loop():
    """
    Background loop that processes jobs from job_queue one at a time.
    Each job is (image_bytes, filename).
    """
    logging.info("QueueWorker: starting background queue worker loop...")
    while True:
        try:
            image_bytes, filename = job_queue.get()
            logging.info(
                "QueueWorker: picked up job for filename %s (queue size=%d)",
                filename,
                job_queue.qsize(),
            )
            _process_and_upload(image_bytes, filename)
        except Exception:
            logging.exception("QueueWorker: error during job processing.")
        finally:
            job_queue.task_done()


# Start the worker thread when the module is imported
_worker_thread = threading.Thread(
    target=_queue_worker_loop,
    name="queue-worker-thread",
    daemon=True,
)
_worker_thread.start()

# -------------------------------------------------
# helpers
# -------------------------------------------------
def _read_texts(paths: List[str]) -> str:
    parts: List[str] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                parts.append(f.read())
        except Exception:
            logging.exception("Failed reading %s", p)
    return "\n".join(parts)


def _load_pil_from_bytes(data: bytes) -> Optional[PILImage]:
    if data is None:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        return ImageOps.exif_transpose(img)
    except Exception:
        return None


def _openai_process(prompt_text: str, image: Optional[PILImage]) -> str:
    content = []
    if prompt_text:
        content.append({"type": "input_text", "text": prompt_text})
    if isinstance(image, PILImage):
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        b64 = b64encode(buf.getvalue()).decode("utf-8")
        content.append(
            {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"}
        )

    resp = client.responses.create(
        model="gpt-5",
        reasoning={"effort": "low"},
        text={"verbosity": "low"},
        tools=[{"type": "web_search"}],
        input=[{"role": "user", "content": content}] if content else prompt_text,
    )

    text = getattr(resp, "output_text", None)
    if not text:
        try:
            text = resp.output[0].content[0].text
        except Exception:
            text = str(resp)
    return text


def add_hyperlink(paragraph, text, url):
    """
    Add a hyperlink to a paragraph. Returns the hyperlink run.
    """
    part = paragraph.part
    r_id = part.relate_to(
        url,
        reltype=(
            "http://schemas.openxmlformats.org/officeDocument/2006/"
            "relationships/hyperlink"
        ),
        is_external=True,
    )

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")

    rPr = OxmlElement("w:rPr")

    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0000FF")
    rPr.append(color)

    new_run.append(rPr)

    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)

    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)

    return new_run


def _docx_bytes_from_text(
    text: str,
    image: Optional[PILImage],
    title: str = "Donation Audit",
) -> bytes:
    doc = Document()
    if title:
        doc.add_heading(title, level=0)

    # optional image at top
    if image is not None:
        img_buf = io.BytesIO()
        image.save(img_buf, format="PNG")
        img_buf.seek(0)
        section = doc.sections[0]
        max_width = (
            section.page_width - section.left_margin - section.right_margin
        )
        doc.add_picture(img_buf, width=max_width)
        doc.add_paragraph("")

    for line in (text or "").splitlines():
        p = doc.add_paragraph()
        pos = 0
        for match in LINK_RE.finditer(line):
            start, end = match.span()
            link_text = match.group(1)
            link_url = match.group(2)

            if start > pos:
                p.add_run(line[pos:start])

            add_hyperlink(p, link_text, link_url)
            pos = end

        if pos < len(line):
            p.add_run(line[pos:])

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def get_box_client_from_config(config_path: str = "config.json") -> BoxClient:
    """
    Build a Box client using ONLY config.json (no Azure env vars).

    If your config.json includes a "sub" key for the user ID, we
    impersonate that user; otherwise we act as the service account.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        cfg_json = json.load(f)

    user_id = cfg_json.get("sub")

    jwt_cfg = JWTConfig.from_config_file(
        config_file_path=config_path,
        token_storage=FileWithInMemoryCacheTokenStorage(".user.jwt.cache"),
    )

    # Start from app/service-auth context
    auth = BoxJWTAuth(jwt_cfg)

    # If a user subject is configured, impersonate that user
    if user_id:
        auth = auth.with_user_subject(user_id)

    client = BoxClient(auth)
    return client


def upload_docx_bytes(
    client: BoxClient,
    file_name: str,
    docx_bytes: bytes,
    folder_id: str = "0",
):
    stream = io.BytesIO(docx_bytes)
    attrs = UploadFileAttributes(
        name=file_name, parent=UploadFileAttributesParentField(id=folder_id)
    )
    try:
        res = client.uploads.upload_file(attrs, stream)
        uploaded = res.entries[0]
        return uploaded.id, uploaded.name
    except Exception as err:
        from box_sdk_gen import BoxAPIError

        if isinstance(err, BoxAPIError) and err.response_info.body.get(
            "code"
        ) == "item_name_in_use":
            conflicts = err.response_info.body["context_info"]["conflicts"]
            box_file_id = conflicts["id"]
            stream.seek(0)
            res = client.uploads.upload_file_version(box_file_id, stream, attrs)
            uploaded = res.entries[0]
            return uploaded.id, uploaded.name
        raise


# -------------------------------------------------
# background processing
# -------------------------------------------------
def _process_and_upload(
    image_bytes: bytes,
    filename: str,
) -> None:
    """
    Background job: run OpenAI, build DOCX, upload to Box.
    Runs inside the queue worker thread.
    """
    try:
        logging.info("BG: starting background processing...")

        # Rebuild the image from bytes
        image = _load_pil_from_bytes(image_bytes)
        if image is None:
            logging.error("BG: could not decode image in background task.")
            return

        # Build prompt
        system_prompt = _read_texts([PROMPT_PATH])
        guides_text = _read_texts([DOC1_PATH, DOC2_PATH])
        full_prompt = (system_prompt or "") + "\n" + (guides_text or "")

        # OpenAI
        logging.info("BG: calling OpenAI...")
        out_text = _openai_process(full_prompt, image)
        logging.info("BG: OpenAI call finished.")

        # DOCX
        logging.info("BG: building DOCX...")
        docx_bytes = _docx_bytes_from_text(out_text or "", image)

        # Box upload
        logging.info("BG: building Box client from config.json...")
        box_client = get_box_client_from_config("config.json")

        logging.info("BG: uploading DOCX to Box...")
        file_id, file_name = upload_docx_bytes(
            box_client, filename, docx_bytes, folder_id="0"
        )
        logging.info("BG: upload complete. Box id=%s, name=%s", file_id, file_name)

    except Exception:
        logging.exception("BG: error during background processing/upload.")


# -------------------------------------------------
# API schema + endpoint
# -------------------------------------------------
class ProcessResponse(BaseModel):
    status_message: str
    expected_filename: str


@app.post("/process", response_model=ProcessResponse)
async def process_endpoint(
    file: UploadFile = File(...),
):
    """
    Accept an image, enqueue background processing into an in-memory queue,
    and return an immediate ACK. The heavy work (OpenAI + DOCX + Box upload)
    runs in the queue worker thread, one job at a time.
    """
    logging.info("Worker: /process called, reading file bytes for ACK...")
    raw_bytes = await file.read()
    image = _load_pil_from_bytes(raw_bytes)
    if image is None:
        logging.error("Worker: could not decode image.")
        raise HTTPException(status_code=400, detail="Could not decode image")

    # Compute expected filename up front
    filename = (
        f"GW-IRC-Donation-Value-Audit--"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.docx"
    )
    logging.info(
        "Worker: enqueuing background job for filename %s (queue size before=%d)",
        filename,
        job_queue.qsize(),
    )

    # Enqueue the job instead of using BackgroundTasks
    job_queue.put((raw_bytes, filename))

    status_message = (
        "The worker has received your image and queued processing. "
        "Requests are processed in the order they are received. "
        "It is now safe to close this tab. "
        "Your document will be uploaded to Box under the filename shown."
    )

    # Immediate ACK — OpenAI / Box will run later in the queue worker
    return ProcessResponse(
        status_message=status_message,
        expected_filename=filename,
    )
