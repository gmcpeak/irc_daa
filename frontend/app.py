# frontend/app.py
import os
import time
import dotenv
import requests
import streamlit as st

dotenv.load_dotenv(".env")

WORKER_URL = os.getenv("WORKER_URL", "http://localhost:8000")

POLL_EVERY_SEC = float(os.getenv("POLL_EVERY_SEC", "2"))
ESTIMATED_TOTAL_SEC = float(os.getenv("ESTIMATED_TOTAL_SEC", "90"))

STATUS_TIMEOUT_SEC = float(os.getenv("STATUS_TIMEOUT_SEC", "5"))
DOWNLOAD_TIMEOUT_SEC = float(os.getenv("DOWNLOAD_TIMEOUT_SEC", "30"))

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

st.set_page_config(page_title="GW+IRC DAA", layout="wide")


# -------------------------------------------------
# session state
# -------------------------------------------------
def _ss_init(key, default):
    if key not in st.session_state:
        st.session_state[key] = default


_ss_init("job_id", None)
_ss_init("job_submitted_at", None)
_ss_init("expected_filename", None)
_ss_init("job_status", None)
_ss_init("job_error", None)
_ss_init("box_file_id", None)
_ss_init("box_file_name", None)

_ss_init("docx_bytes", None)
_ss_init("output_text", None)
_ss_init("result_for_job_id", None)  # ensure we don't mix results across jobs

_ss_init("last_ack_message", None)

# NEW: bump this to reset camera/uploader widgets
_ss_init("input_key_version", 0)


def _reset_for_new_job():
    st.session_state.job_id = None
    st.session_state.job_submitted_at = None
    st.session_state.expected_filename = None
    st.session_state.job_status = None
    st.session_state.job_error = None
    st.session_state.box_file_id = None
    st.session_state.box_file_name = None

    st.session_state.docx_bytes = None
    st.session_state.output_text = None
    st.session_state.result_for_job_id = None

    st.session_state.last_ack_message = None


def _bump_input_widgets():
    """
    Force Streamlit to recreate the camera_input + file_uploader widgets by changing keys.
    This reliably resets their UI/state. :contentReference[oaicite:2]{index=2}
    """
    st.session_state.input_key_version += 1


def _wkey(base: str) -> str:
    return f"{base}_{st.session_state.input_key_version}"


# -------------------------------------------------
# worker API helpers
# -------------------------------------------------
def _post_process(filename: str, file_bytes: bytes, mime_type: str) -> dict:
    resp = requests.post(
        f"{WORKER_URL}/process",
        files={"file": (filename, file_bytes, mime_type)},
        timeout=30,
    )
    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = body.get("detail", str(body))
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Worker error (status {resp.status_code}): {detail}")
    return resp.json()


def _get_status(job_id: str) -> dict:
    resp = requests.get(f"{WORKER_URL}/jobs/{job_id}", timeout=STATUS_TIMEOUT_SEC)
    if resp.status_code == 404:
        raise RuntimeError("Job not found on worker (expired, restarted, or invalid job_id).")
    if resp.status_code != 200:
        raise RuntimeError(f"Status check failed (status {resp.status_code}): {resp.text}")
    return resp.json()


def _get_docx(job_id: str) -> bytes:
    resp = requests.get(f"{WORKER_URL}/jobs/{job_id}/docx", timeout=DOWNLOAD_TIMEOUT_SEC)
    if resp.status_code == 202:
        raise RuntimeError("Not ready yet")
    if resp.status_code == 404:
        raise RuntimeError("Job not found on worker.")
    if resp.status_code != 200:
        raise RuntimeError(f"Download failed (status {resp.status_code}): {resp.text}")
    return resp.content


def _get_text(job_id: str) -> str:
    resp = requests.get(f"{WORKER_URL}/jobs/{job_id}/text", timeout=DOWNLOAD_TIMEOUT_SEC)
    if resp.status_code == 202:
        raise RuntimeError("Not ready yet")
    if resp.status_code == 404:
        raise RuntimeError("Job not found on worker.")
    if resp.status_code != 200:
        raise RuntimeError(f"Text fetch failed (status {resp.status_code}): {resp.text}")
    return resp.text


def _status_badge(status: str | None) -> str:
    if not status:
        return "—"
    s = status.lower()
    if s == "queued":
        return "🟦 queued"
    if s == "running":
        return "🟨 running"
    if s == "done":
        return "🟩 done"
    if s == "failed":
        return "🟥 failed"
    return status


def _compute_progress_fraction(status: str | None, submitted_at: float | None) -> float:
    if not status:
        return 0.0
    s = status.lower()
    if s == "queued":
        return 0.10
    if s == "running":
        if not submitted_at:
            return 0.35
        elapsed = max(0.0, time.time() - submitted_at)
        ramp = min(0.95, 0.15 + (elapsed / max(1.0, ESTIMATED_TOTAL_SEC)) * 0.80)
        return ramp
    if s in ("done", "failed"):
        return 1.0
    return 0.0


# -------------------------------------------------
# UI
# -------------------------------------------------
st.markdown("# GW+IRC Donation Audit Assistant")
st.markdown(
    "Take a picture **or** upload one, then click **Process**. "
    "Your AI-generated assessment will be automatically uploaded to Box, as well as made available here."
)

left, right = st.columns(2)

# IMPORTANT: use versioned keys so we can reset these widgets after Process
with left:
    cam = st.camera_input("Take a photo", key=_wkey("camera"))
with right:
    upl = st.file_uploader(
        "...or upload a photo",
        type=["png", "jpg", "jpeg"],
        key=_wkey("uploader"),
    )

uploaded = cam or upl

ack_area = st.empty()
status_area = st.empty()
progress_area = st.empty()
result_area = st.empty()


# -------------------------------------------------
# Submit
# -------------------------------------------------
if st.button("Process", type="primary", disabled=(uploaded is None)):
    if uploaded is None:
        ack_area.warning("Please take or upload a photo first.")
    else:
        # Capture bytes BEFORE we reset widget keys
        file_bytes = uploaded.getvalue()
        filename = getattr(uploaded, "name", None) or "image.png"
        mime_type = getattr(uploaded, "type", None) or "image/png"

        # Reset tracking to the newest job
        _reset_for_new_job()

        with st.spinner("Submitting image to worker..."):
            try:
                data = _post_process(filename, file_bytes, mime_type)
            except Exception as e:
                ack_area.error(f"Failed to submit image: {e}")
                data = None

        if data:
            st.session_state.job_id = data.get("job_id")
            st.session_state.expected_filename = data.get("expected_filename")
            st.session_state.job_status = "queued"
            st.session_state.job_error = None
            st.session_state.job_submitted_at = time.time()
            st.session_state.last_ack_message = data.get("status_message")

            lines = ["✅ Image submitted to worker."]
            if st.session_state.expected_filename:
                lines.append(f"Expected filename: **{st.session_state.expected_filename}**")
            if st.session_state.job_id:
                lines.append(f"Job ID: `{st.session_state.job_id}`")
            if st.session_state.last_ack_message:
                lines.append(st.session_state.last_ack_message)

            ack_area.success("  \n".join(lines))

        # NEW: reset camera/uploader widgets so users can submit another image immediately
        _bump_input_widgets()

        # Immediately rerun so the cleared widgets appear right away
        st.rerun()


# -------------------------------------------------
# Auto-polling fragment (updates status/progress/results without extra buttons)
# -------------------------------------------------
@st.fragment(run_every=POLL_EVERY_SEC)
def _poll_and_render():
    job_id = st.session_state.job_id

    if not job_id:
        status_area.info("Submit an image to begin.")
        progress_area.empty()
        result_area.empty()
        return

    # If we already fetched results for this job, render them.
    if st.session_state.result_for_job_id == job_id and st.session_state.docx_bytes is not None:
        status_area.success(f"Worker status: **{_status_badge(st.session_state.job_status)}**")
        progress_area.progress(1.0, text="Complete")

        st.download_button(
            "Download as Word (.docx)",
            data=st.session_state.docx_bytes,
            file_name=st.session_state.expected_filename or "Donation-Audit.docx",
            mime=DOCX_MIME,
            key=f"download_{job_id}",
        )

        if st.session_state.box_file_id or st.session_state.box_file_name:
            st.caption(
                f"Box upload: {st.session_state.box_file_name or '(name unknown)'} "
                f"(id={st.session_state.box_file_id or 'unknown'})"
            )

        st.markdown("### Generated output text")
        st.markdown(st.session_state.output_text or "")
        return

    # Poll status
    try:
        sdata = _get_status(job_id)
        st.session_state.job_status = sdata.get("status")
        st.session_state.job_error = sdata.get("error")
        st.session_state.expected_filename = sdata.get("expected_filename") or st.session_state.expected_filename
        st.session_state.box_file_id = sdata.get("box_file_id")
        st.session_state.box_file_name = sdata.get("box_file_name")
    except Exception as e:
        status_area.warning(f"Status check error: {e}")
        frac = _compute_progress_fraction(st.session_state.job_status, st.session_state.job_submitted_at)
        progress_area.progress(frac, text="Checking status…")
        return

    status = st.session_state.job_status
    badge = _status_badge(status)

    if status == "failed":
        status_area.error(f"Worker status: **{badge}**")
        progress_area.progress(1.0, text="Failed")
        if st.session_state.job_error:
            result_area.error(st.session_state.job_error)
        return

    frac = _compute_progress_fraction(status, st.session_state.job_submitted_at)
    status_area.info(f"Worker status: **{badge}**")
    progress_area.progress(frac, text=f"{badge}")

    # When done, fetch docx + text once
    if status == "done":
        with st.spinner("Fetching results…"):
            try:
                docx = _get_docx(job_id)
                text = _get_text(job_id)
            except Exception as e:
                result_area.warning(f"Result not retrievable yet: {e}")
                return

        st.session_state.docx_bytes = docx
        st.session_state.output_text = text
        st.session_state.result_for_job_id = job_id

        status_area.success("✅ Complete")
        progress_area.progress(1.0, text="Complete")

        st.download_button(
            "Download as Word (.docx)",
            data=st.session_state.docx_bytes,
            file_name=st.session_state.expected_filename or "Donation-Audit.docx",
            mime=DOCX_MIME,
            key=f"download_{job_id}",
        )

        st.markdown("### Generated output text")
        st.markdown(st.session_state.output_text or "")


_poll_and_render()
