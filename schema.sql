-- MSOE canonical schema.
-- Safe to run against a greenfield or existing database: every statement is
-- CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS.  Never DROPs data.

-- ---------------------------------------------------------------------------
-- students
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS students (
    student_id         BIGSERIAL PRIMARY KEY,
    school_id          VARCHAR(64)  NOT NULL DEFAULT 'default',
    source_student_id  VARCHAR(64),
    emirates_id        VARCHAR(15),
    first_name         VARCHAR(64)  NOT NULL,
    last_name          VARCHAR(64)  NOT NULL,
    gender             VARCHAR(10)  NOT NULL,
    date_of_birth      DATE         NOT NULL,
    grade_level        SMALLINT     NOT NULL,
    curriculum_track   VARCHAR(16)  NOT NULL,
    school_name        VARCHAR(128) NOT NULL,
    emirate            VARCHAR(32)  NOT NULL,
    nationality        VARCHAR(64)  NOT NULL,
    parent_full_name   VARCHAR(128) NOT NULL,
    parent_email       VARCHAR(128) NOT NULL,
    parent_phone       VARCHAR(20)  NOT NULL,
    vaccination_status VARCHAR(16)  NOT NULL,
    vaccination_expiry DATE,
    enrollment_date    DATE         NOT NULL,
    is_active          BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT students_grade_level_check     CHECK (grade_level BETWEEN 1 AND 12),
    CONSTRAINT students_curriculum_track_check CHECK (curriculum_track IN ('British','IB','CBSE','MoE')),
    CONSTRAINT students_vaccination_status_check CHECK (vaccination_status IN ('up_to_date','partial','expired','missing'))
);

-- Migration for tables created before school_id / source_student_id existed.
ALTER TABLE students ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE students ADD COLUMN IF NOT EXISTS source_student_id VARCHAR(64);
-- The old mock hash-synthesised emirates_id occupied a NOT NULL UNIQUE slot.
-- A missing national ID is NULL — never a fabricated one.
ALTER TABLE students ALTER COLUMN emirates_id DROP NOT NULL;

-- Replace global-UNIQUE (emirates_id) with per-school composite.
ALTER TABLE students DROP CONSTRAINT IF EXISTS students_emirates_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS students_school_emirates_uidx
    ON students (school_id, emirates_id);
CREATE UNIQUE INDEX IF NOT EXISTS students_school_source_uidx
    ON students (school_id, source_student_id);
CREATE INDEX IF NOT EXISTS idx_students_curriculum ON students (curriculum_track);
CREATE INDEX IF NOT EXISTS idx_students_grade      ON students (grade_level);

-- Back-fill source_student_id from the old PK (it held the SIS-native id).
UPDATE students SET source_student_id = student_id::text
WHERE source_student_id IS NULL;

-- ---------------------------------------------------------------------------
-- attendance_logs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attendance_logs (
    attendance_id   BIGSERIAL PRIMARY KEY,
    school_id       VARCHAR(64)  NOT NULL DEFAULT 'default',
    student_id      BIGINT       NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    attendance_date DATE         NOT NULL,
    status          VARCHAR(16)  NOT NULL,
    minutes_present SMALLINT     NOT NULL DEFAULT 0,
    notes           VARCHAR(255),
    CONSTRAINT attendance_logs_status_check CHECK (status IN ('present','absent','late','excused'))
);

ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
CREATE UNIQUE INDEX IF NOT EXISTS attendance_logs_student_date_uidx
    ON attendance_logs (student_id, attendance_date);
CREATE INDEX IF NOT EXISTS idx_attendance_status ON attendance_logs (status);

-- ---------------------------------------------------------------------------
-- fee_ledgers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fee_ledgers (
    fee_id         BIGSERIAL PRIMARY KEY,
    school_id      VARCHAR(64)   NOT NULL DEFAULT 'default',
    student_id     BIGINT        NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    invoice_number VARCHAR(32)   NOT NULL,
    term_label     VARCHAR(32)   NOT NULL,
    amount_due     NUMERIC(12,2) NOT NULL,
    amount_paid    NUMERIC(12,2) NOT NULL DEFAULT 0,
    currency       VARCHAR(3)    NOT NULL DEFAULT 'AED',
    due_date       DATE          NOT NULL,
    paid_date      DATE,
    status         VARCHAR(16)   NOT NULL,
    days_overdue   INTEGER       NOT NULL DEFAULT 0,
    notes          VARCHAR(255),
    CONSTRAINT fee_ledgers_amount_due_check  CHECK (amount_due  >= 0),
    CONSTRAINT fee_ledgers_amount_paid_check CHECK (amount_paid >= 0),
    CONSTRAINT fee_ledgers_status_check CHECK (status IN ('current','partial','overdue','paid','waived'))
);

ALTER TABLE fee_ledgers ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
-- invoice_number was globally UNIQUE — two schools both emitting INV-1 collided.
ALTER TABLE fee_ledgers DROP CONSTRAINT IF EXISTS fee_ledgers_invoice_number_key;
CREATE UNIQUE INDEX IF NOT EXISTS fee_ledgers_school_invoice_uidx
    ON fee_ledgers (school_id, invoice_number);
CREATE INDEX IF NOT EXISTS idx_fees_status  ON fee_ledgers (status);
CREATE INDEX IF NOT EXISTS idx_fees_student ON fee_ledgers (student_id);

-- ---------------------------------------------------------------------------
-- staff
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    staff_id        SERIAL PRIMARY KEY,
    school_id       VARCHAR(64)  NOT NULL DEFAULT 'default',
    source_staff_id VARCHAR(64),
    full_name       VARCHAR(128) NOT NULL,
    email           VARCHAR(128) NOT NULL,
    role            VARCHAR(32)  NOT NULL DEFAULT 'teacher',
    subjects        TEXT[]       NOT NULL DEFAULT '{}',
    is_available    BOOLEAN      NOT NULL DEFAULT TRUE,
    max_periods     SMALLINT     NOT NULL DEFAULT 6,
    assigned        SMALLINT     NOT NULL DEFAULT 0
);

ALTER TABLE staff ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE staff ADD COLUMN IF NOT EXISTS source_staff_id VARCHAR(64);
ALTER TABLE staff DROP CONSTRAINT IF EXISTS staff_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS staff_school_email_uidx
    ON staff (school_id, email);
CREATE UNIQUE INDEX IF NOT EXISTS staff_school_source_uidx
    ON staff (school_id, source_staff_id);

-- Back-fill source_staff_id from the old PK.
UPDATE staff SET source_staff_id = staff_id::text
WHERE source_staff_id IS NULL;

-- ---------------------------------------------------------------------------
-- leave_requests
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leave_requests (
    leave_id   SERIAL PRIMARY KEY,
    school_id  VARCHAR(64)  NOT NULL DEFAULT 'default',
    staff_id   INTEGER      NOT NULL REFERENCES staff(staff_id),
    leave_date DATE         NOT NULL,
    reason     VARCHAR(255),
    status     VARCHAR(32)  NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL',
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';

-- ---------------------------------------------------------------------------
-- roi_events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roi_events (
    roi_event_id  BIGSERIAL PRIMARY KEY,
    school_id     VARCHAR(64)  NOT NULL DEFAULT 'default',
    engagement_id VARCHAR(64)  NOT NULL DEFAULT 'default',
    service       VARCHAR(64)  NOT NULL,
    unit          VARCHAR(64)  NOT NULL,
    qty           NUMERIC(12,2) NOT NULL DEFAULT 1,
    minutes_each  NUMERIC(8,2)  NOT NULL,
    role          VARCHAR(32)  NOT NULL,
    minutes_total NUMERIC(12,2) NOT NULL,
    detail        JSONB,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE roi_events ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_roi_events_engagement ON roi_events (engagement_id);
CREATE INDEX IF NOT EXISTS idx_roi_events_service    ON roi_events (service);
CREATE INDEX IF NOT EXISTS idx_roi_events_created    ON roi_events (created_at);

-- ---------------------------------------------------------------------------
-- approval_actions  (durable ApprovalStore)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_actions (
    action_id       VARCHAR(16)  PRIMARY KEY,
    school_id       VARCHAR(64)  NOT NULL DEFAULT 'default',
    action          VARCHAR(64)  NOT NULL,
    category        VARCHAR(32)  NOT NULL DEFAULT 'WRITE',
    reason          TEXT         NOT NULL,
    payload         JSONB        NOT NULL DEFAULT '{}',
    status          VARCHAR(32)  NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolution      VARCHAR(32),
    executed_result JSONB
);

ALTER TABLE approval_actions ADD COLUMN IF NOT EXISTS school_id VARCHAR(64) NOT NULL DEFAULT 'default';
CREATE SEQUENCE IF NOT EXISTS approval_actions_seq START 1;
CREATE INDEX IF NOT EXISTS idx_approval_actions_status ON approval_actions (status);
CREATE INDEX IF NOT EXISTS idx_approval_actions_school  ON approval_actions (school_id);
