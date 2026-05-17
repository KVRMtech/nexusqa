"""Media processing tables — audio_files, transcript_segments, video_files, visual_frames, media_processing_jobs

Revision ID: 004_media_processing
Revises: 003_test_cases
Create Date: 2026-04-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_media_processing"
down_revision: Union[str, None] = "003_test_cases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Audio Files ────────────────────────────────────────
    op.create_table(
        "audio_files",
        sa.Column("audio_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        # File info
        sa.Column("original_filename", sa.String(500), server_default=""),
        sa.Column("file_path", sa.String(1000), nullable=False),
        sa.Column("processed_path", sa.String(1000), server_default=""),
        sa.Column("format", sa.String(20), server_default="wav"),
        sa.Column("file_size_bytes", sa.Integer, server_default="0"),
        # Technical metadata
        sa.Column("sample_rate", sa.Integer, server_default="16000"),
        sa.Column("channels", sa.Integer, server_default="1"),
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),
        sa.Column("bit_depth", sa.Integer, server_default="16"),
        sa.Column("codec", sa.String(50), server_default="pcm_s16le"),
        # Preprocessing flags
        sa.Column("normalized", sa.Boolean, server_default=sa.text("false")),
        sa.Column("noise_reduced", sa.Boolean, server_default=sa.text("false")),
        sa.Column("resampled_from", sa.Integer, nullable=True),
        # Processing state
        sa.Column("preprocess_stages", sa.JSON, server_default="[]"),
        sa.Column("chunk_count", sa.Integer, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audio_files_session", "audio_files", ["session_id"])
    op.create_index("ix_audio_files_job", "audio_files", ["job_id"])
    op.create_index(
        "ix_audio_files_tenant_session", "audio_files", ["tenant_id", "session_id"]
    )

    # ── Transcript Segments ────────────────────────────────
    op.create_table(
        "transcript_segments",
        sa.Column("segment_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column(
            "audio_id",
            sa.String(64),
            sa.ForeignKey("audio_files.audio_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Content
        sa.Column("speaker", sa.String(100), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("start_time", sa.Float, nullable=False),
        sa.Column("end_time", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, server_default="0.0"),
        sa.Column("language", sa.String(10), server_default="en"),
        # Word-level timing
        sa.Column("words_json", sa.JSON, server_default="[]"),
        # Ordering
        sa.Column("segment_index", sa.Integer, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_transcript_segments_session",
        "transcript_segments",
        ["session_id", "segment_index"],
    )
    op.create_index(
        "ix_transcript_segments_tenant",
        "transcript_segments",
        ["tenant_id", "session_id"],
    )
    op.create_index(
        "ix_transcript_segments_speaker",
        "transcript_segments",
        ["session_id", "speaker"],
    )

    # ── Video Files ────────────────────────────────────────
    op.create_table(
        "video_files",
        sa.Column("video_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        # File info
        sa.Column("original_filename", sa.String(500), server_default=""),
        sa.Column("file_path", sa.String(1000), nullable=False),
        sa.Column("format", sa.String(20), server_default="mp4"),
        sa.Column("file_size_bytes", sa.Integer, server_default="0"),
        # Video metadata
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),
        sa.Column("width", sa.Integer, server_default="0"),
        sa.Column("height", sa.Integer, server_default="0"),
        sa.Column("fps", sa.Float, server_default="30.0"),
        sa.Column("codec", sa.String(50), server_default="h264"),
        # Processing
        sa.Column("total_frames_extracted", sa.Integer, server_default="0"),
        sa.Column("total_frames_analyzed", sa.Integer, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_video_files_session", "video_files", ["session_id"])
    op.create_index("ix_video_files_job", "video_files", ["job_id"])
    op.create_index(
        "ix_video_files_tenant_session", "video_files", ["tenant_id", "session_id"]
    )

    # ── Visual Frames ──────────────────────────────────────
    op.create_table(
        "visual_frames",
        sa.Column("frame_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column(
            "video_id",
            sa.String(64),
            sa.ForeignKey("video_files.video_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Frame position
        sa.Column("frame_index", sa.Integer, server_default="0"),
        sa.Column("timestamp_seconds", sa.Float, server_default="0.0"),
        sa.Column("frame_path", sa.String(1000), server_default=""),
        # Classification
        sa.Column("application_type", sa.String(50), server_default="unknown"),
        sa.Column("page_title", sa.String(500), server_default=""),
        sa.Column("url_or_path", sa.String(1000), server_default=""),
        # Content
        sa.Column("ui_elements_json", sa.JSON, server_default="[]"),
        sa.Column("extracted_text", sa.Text, server_default=""),
        sa.Column("tables_json", sa.JSON, server_default="[]"),
        sa.Column("state_changes_json", sa.JSON, server_default="[]"),
        sa.Column("description", sa.Text, server_default=""),
        # Quality
        sa.Column("ocr_confidence", sa.Float, server_default="0.0"),
        sa.Column("is_keyframe", sa.Boolean, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_visual_frames_session", "visual_frames", ["session_id", "frame_index"]
    )
    op.create_index(
        "ix_visual_frames_tenant", "visual_frames", ["tenant_id", "session_id"]
    )
    op.create_index("ix_visual_frames_video", "visual_frames", ["video_id"])

    # ── Media Processing Jobs ──────────────────────────────
    op.create_table(
        "media_processing_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), server_default="queued"),
        # Input
        sa.Column("source_file_path", sa.String(1000), server_default=""),
        sa.Column("original_filename", sa.String(500), server_default=""),
        # Parameters
        sa.Column("language", sa.String(10), server_default="en"),
        sa.Column("num_speakers", sa.Integer, nullable=True),
        sa.Column("parameters_json", sa.JSON, server_default="{}"),
        # Progress
        sa.Column("progress_percent", sa.Float, server_default="0.0"),
        sa.Column("current_stage", sa.String(50), server_default="queued"),
        sa.Column("pipeline_stages", sa.JSON, server_default="[]"),
        # Result summary
        sa.Column("segment_count", sa.Integer, server_default="0"),
        sa.Column("speaker_count", sa.Integer, server_default="0"),
        sa.Column("frame_count", sa.Integer, server_default="0"),
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),
        sa.Column("word_count", sa.Integer, server_default="0"),
        # Error
        sa.Column("error", sa.Text, nullable=True),
        # Timing
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_time_seconds", sa.Float, server_default="0.0"),
    )
    op.create_index(
        "ix_media_jobs_tenant_status",
        "media_processing_jobs",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_media_jobs_session", "media_processing_jobs", ["session_id"]
    )
    op.create_index(
        "ix_media_jobs_type", "media_processing_jobs", ["tenant_id", "job_type"]
    )


def downgrade() -> None:
    op.drop_table("media_processing_jobs")
    op.drop_table("visual_frames")
    op.drop_table("video_files")
    op.drop_table("transcript_segments")
    op.drop_table("audio_files")
