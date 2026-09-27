"""E — send the current working copy to a remote enhancer.

The returned image becomes a **new pending proposal** with
`operations.ops.remote_enhance`, reviewed exactly like an analysed one. It is
never auto-accepted. Spend is recorded in `cleanup_spend` before the call
completes, so a crash mid-job still shows up in the counter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ... import db
from ...config import Settings
from . import paths as cpaths
from . import render as render_mod
from . import repo
from .accept import CleanupError, install_new_version
from .geometry import Transform
from .remote import RemoteUnavailable, build_provider

log = logging.getLogger(__name__)

# What we upload: JPEG at this quality, full resolution. A provider that hands
# back something smaller is recorded as such and shows up in the caption, so
# George can decline a downscale rather than discover it later.
UPLOAD_QUALITY = 95


@dataclass
class RemoteResult:
    photo_id: int
    proposal_id: int
    provider: str
    job_ref: str
    cost_estimate_usd: float
    image_path: str
    width: int
    height: int
    source_width: int
    source_height: int

    @property
    def downscaled(self) -> bool:
        return (self.width, self.height) != (self.source_width, self.source_height)

    def summary(self) -> str:
        bit = f"photo {self.photo_id}: {self.provider} returned {self.width}×{self.height}"
        if self.downscaled:
            bit += f" (source was {self.source_width}×{self.source_height})"
        return bit + f", ${self.cost_estimate_usd:.2f}"


def send_to_remote(
    settings: Settings, proposal_id: int, *, actor: str = "desktop",
) -> RemoteResult:
    provider = build_provider(settings)
    if not provider.available:
        raise RemoteUnavailable(provider.unavailable_reason() or "no provider")

    with db.connection() as conn:
        conn.autocommit = True
        proposal = repo.load_proposal(conn, proposal_id)
    if proposal is None:
        raise CleanupError(f"proposal {proposal_id} not found")
    src = proposal.resolved_path(settings)
    if src is None or not src.exists():
        raise CleanupError(f"working file missing for photo {proposal.photo_id}")

    cpaths.ensure_dirs(settings)
    upload = cpaths.remote_path(settings, proposal.photo_id, "upload", "jpg")
    with Image.open(src) as im:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im).convert("RGB")
        src_w, src_h = im.size
        upload.parent.mkdir(parents=True, exist_ok=True)
        im.save(upload, "JPEG", quality=UPLOAD_QUALITY, subsampling=0)

    job = provider.submit(upload)
    with db.connection() as conn:
        conn.autocommit = True
        spend_id = repo.record_spend(
            conn, provider=provider.name, photo_id=proposal.photo_id,
            proposal_id=proposal_id, job_ref=job.job_ref,
            cost_estimate_usd=job.cost_estimate_usd, status="submitted",
        )

    out = cpaths.remote_path(settings, proposal.photo_id, job.job_ref, "jpg")
    try:
        provider.await_result(job, out)
    except Exception:
        with db.connection() as conn:
            conn.autocommit = True
            conn.execute("update cleanup_spend set status = 'failed' where id = %s",
                         (spend_id,))
        raise

    with Image.open(out) as im:
        out_w, out_h = im.size

    operations: dict[str, Any] = {
        "analysis": {"src_w": src_w, "src_h": src_h},
        "ops": {"remote_enhance": {
            "provider": provider.name,
            "job_ref": job.job_ref,
            "image_path": str(out),
            "out_w": out_w, "out_h": out_h,
            "cost_estimate_usd": job.cost_estimate_usd,
            "downscaled": (out_w, out_h) != (src_w, src_h),
        }},
    }

    with db.connection() as conn:
        conn.autocommit = False
        try:
            conn.execute(
                """
                update cleanup_spend
                   set status = 'completed', actual_cost_usd = coalesce(actual_cost_usd, %s)
                 where id = %s
                """,
                (job.cost_estimate_usd, spend_id),
            )
            repo.supersede_pending(conn, proposal.photo_id)
            new_id = repo.insert_proposal(
                conn, photo_id=proposal.photo_id, status="pending",
                operations=operations, transform=None, split_regions=None,
                needs_manual=False, manual_reason=None, analysis_ms=None,
                derived_path=str(out),
            )
            db.audit(conn, actor=actor, action="cleanup.remote",
                     entity_type="photo", entity_id=proposal.photo_id,
                     previous_value={"proposal_id": proposal_id},
                     new_value={"proposal_id": new_id,
                                "provider": provider.name,
                                "job_ref": job.job_ref,
                                "cost_estimate_usd": job.cost_estimate_usd,
                                "out_w": out_w, "out_h": out_h,
                                "spend_id": spend_id})
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return RemoteResult(
        photo_id=proposal.photo_id, proposal_id=new_id, provider=provider.name,
        job_ref=job.job_ref, cost_estimate_usd=job.cost_estimate_usd,
        image_path=str(out), width=out_w, height=out_h,
        source_width=src_w, source_height=src_h,
    )


def is_remote_proposal(proposal: repo.Proposal) -> bool:
    return "remote_enhance" in ((proposal.operations or {}).get("ops") or {})


def accept_remote(
    settings: Settings, proposal: repo.Proposal, *, actor: str = "desktop",
) -> Any:
    """Install a remote result as the new working copy.

    The provider already produced the pixels, so there is nothing to render —
    but it may have resized, so the face boxes go through a scale transform
    rather than the identity.
    """
    op = ((proposal.operations or {}).get("ops") or {}).get("remote_enhance") or {}
    image_path = Path(op.get("image_path") or "")
    if not image_path.exists():
        raise CleanupError(
            f"remote result missing for photo {proposal.photo_id}: {image_path}"
        )
    src = proposal.resolved_path(settings)
    if src is None or not src.exists():
        raise CleanupError(f"working file missing for photo {proposal.photo_id}")

    analysis = (proposal.operations or {}).get("analysis") or {}
    src_w = int(analysis.get("src_w") or proposal.width or 0)
    src_h = int(analysis.get("src_h") or proposal.height or 0)
    out_w, out_h = int(op.get("out_w") or 0), int(op.get("out_h") or 0)
    if not (src_w and src_h and out_w and out_h):
        raise CleanupError("remote proposal is missing its dimensions")

    sx, sy = out_w / src_w, out_h / src_h
    transform = Transform(
        m=(sx, 0.0, 0.0, 0.0, sy, 0.0),
        src_w=src_w, src_h=src_h, out_w=out_w, out_h=out_h,
        notes={"remote_enhance": op.get("provider")},
    )

    ext = cpaths.ext_for(proposal.mime, fallback=src.suffix.lstrip(".") or "jpg")
    new_version = int(proposal.file_version) + 1
    derived = cpaths.derived_path(settings, proposal.photo_id, new_version, ext)
    # Re-encode into the photo's own format (invariant 3) and pick up the
    # hashes and thumbnail on the way through.
    rendered = render_mod.render_full(
        image_path, render_mod.Plan(transform=Transform.identity(out_w, out_h)),
        derived, mime=proposal.mime,
    )
    return install_new_version(
        settings, proposal, rendered, transform=transform,
        ticked=("remote_enhance",), src=src, ext=ext, actor=actor,
    )
