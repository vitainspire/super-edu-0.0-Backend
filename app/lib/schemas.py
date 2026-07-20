"""Pydantic models mirroring backend/src/lib/schemas.ts's zod schemas.
FastAPI validates these automatically from the route's type-annotated
parameter — no manual parseBody() step needed, unlike the Express port."""
import re
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

LEN_SHORT = 50
LEN_NAME = 120
LEN_TOPIC = 300
LEN_TEXT = 2000


class YearPlanTopic(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    topic: str = Field(max_length=LEN_TOPIC)
    description: Optional[str] = Field(default=None, max_length=LEN_TEXT)


class YearPlanSchema(BaseModel):
    topics: list[YearPlanTopic] = Field(min_length=1, max_length=200)
    totalWeeks: int = Field(gt=0, le=60)
    sessionsPerWeek: int = Field(gt=0, le=14)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)


class StudentDoubtSchema(BaseModel):
    classId: str = Field(min_length=1, max_length=36)
    subject: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    question: str = Field(min_length=1, max_length=LEN_TEXT)
    studentName: Optional[str] = Field(default=None, max_length=LEN_NAME)


class PeerPairDissolveSchema(BaseModel):
    id: str = Field(min_length=1, max_length=36)


class PeerPairRequestSchema(BaseModel):
    classId: str = Field(min_length=1, max_length=36)
    targetStudentId: str = Field(min_length=1, max_length=36)
    subject: Optional[str] = Field(default=None, max_length=LEN_TOPIC)


class PeerPairActionSchema(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    action: Literal["accept", "cancel"]


class StudentPollSchema(BaseModel):
    classId: str = Field(min_length=1, max_length=36)
    syllabusTopicId: str = Field(min_length=1, max_length=36)
    topic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    subject: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    response: Literal["understood", "partial", "confused"]


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AcademicEventSchema(BaseModel):
    title: str = Field(min_length=1, max_length=LEN_NAME)
    category: Literal["holiday", "exam", "term"]
    holidaySubtype: Optional[Literal["public", "school", "cultural"]] = None
    countsAsNonWorking: Optional[bool] = None
    startDate: str
    endDate: str
    description: Optional[str] = Field(default=None, max_length=LEN_TEXT)

    @field_validator("startDate", "endDate")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("Expected YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.endDate < self.startDate:
            raise ValueError("endDate must be on or after startDate")
        return self


class SeedHolidaysSchema(BaseModel):
    year: int = Field(ge=2000, le=2100)


class AnnouncementSchema(BaseModel):
    title: str = Field(min_length=1, max_length=LEN_NAME)
    body: str = Field(min_length=1, max_length=LEN_TEXT)
    category: Optional[Literal["general", "exam", "urgent", "holiday"]] = None


# ─── AI-passthrough route schemas ───────────────────────────────────────────

class BriefingAtRiskStudent(BaseModel):
    name: str = Field(max_length=LEN_NAME)
    warning: str = Field(max_length=LEN_TEXT)
    absenteeType: Optional[Literal["rare", "chronic"]] = None
    topic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)


class BriefingLastSession(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    date: str = Field(max_length=LEN_SHORT)
    absentCount: int = Field(ge=0, le=500)
    absentNames: Optional[list[str]] = Field(default=None, max_length=500)


class BriefingClassData(BaseModel):
    grade: str = Field(max_length=LEN_SHORT)
    section: str = Field(max_length=LEN_SHORT)
    studentCount: int = Field(ge=0, le=500)
    nextTopic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    nextSubTopic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    lastSubTopics: Optional[list[str]] = Field(default=None, max_length=20)
    atRiskCount: int = Field(ge=0, le=500)
    atRiskStudents: Optional[list[BriefingAtRiskStudent]] = Field(default=None, max_length=50)
    completedTopics: Optional[int] = Field(default=None, ge=0)
    totalTopics: Optional[int] = Field(default=None, ge=0)
    lastSession: Optional[BriefingLastSession] = None


class BriefingSchema(BaseModel):
    teacherName: str = Field(max_length=LEN_NAME)
    classData: list[BriefingClassData] = Field(max_length=20)


class LessonSnapshot(BaseModel):
    hook: str = Field(max_length=LEN_TEXT)
    realLifeExamples: list[str] = Field(max_length=10)


class CatchupPlanSchema(BaseModel):
    studentName: str = Field(min_length=1, max_length=LEN_NAME)
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    score: Optional[float] = Field(default=None, ge=0, le=100)
    lessonSnapshot: Optional[LessonSnapshot] = None
    studentInterests: Optional[list[str]] = Field(default=None, max_length=20)
    studentGoal: Optional[str] = Field(default=None, max_length=LEN_TEXT)
    learningStyle: Optional[str] = Field(default=None, max_length=LEN_NAME)
    overallAttendanceRate: Optional[float] = Field(default=None, ge=0, le=1)
    topicSessionsTotal: Optional[int] = Field(default=None, ge=0)
    topicSessionsMissed: Optional[int] = Field(default=None, ge=0)
    absenteeType: Optional[Literal["rare", "chronic"]] = None


class ClassPulseStudent(BaseModel):
    name: str = Field(max_length=LEN_NAME)
    avgMastery: float = Field(ge=0, le=1)
    attendanceRate: float = Field(ge=0, le=1)
    interests: list[str] = Field(max_length=20)


class ClassPulseTest(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    avgScore: float = Field(ge=0)
    totalMarks: float = Field(gt=0)


class ClassPulseCoverage(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    status: str = Field(max_length=LEN_SHORT)


class ClassPulseSchema(BaseModel):
    className: str = Field(min_length=1, max_length=LEN_NAME)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    attendanceRate: float = Field(ge=0, le=1)
    students: list[ClassPulseStudent] = Field(max_length=300)
    tests: list[ClassPulseTest] = Field(max_length=200)
    topicCoverage: list[ClassPulseCoverage] = Field(max_length=200)


class EnginePresentStudent(BaseModel):
    name: str = Field(max_length=LEN_NAME)
    interests: list[str] = Field(max_length=20)
    goal: str = Field(max_length=LEN_TEXT)


class EngageSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    totalStudents: int = Field(ge=0, le=500)
    presentStudents: list[EnginePresentStudent] = Field(max_length=300)
    absentNames: list[str] = Field(max_length=300)


class LessonPrepSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    language: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    subtopic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)


class PeerPairStudent(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(max_length=LEN_NAME)
    avgMastery: float = Field(ge=0, le=1)
    interests: list[str] = Field(max_length=20)
    goal: str = Field(max_length=LEN_TEXT)


class PeerPairSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    students: list[PeerPairStudent] = Field(min_length=2, max_length=300)


class PotentialSchema(BaseModel):
    studentName: str = Field(min_length=1, max_length=LEN_NAME)
    signal: dict


class QuestionsSchema(BaseModel):
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    totalMarks: object  # number or short string in the original — kept loose, normalized at use


class PreviousApproach(BaseModel):
    approachUsed: str = Field(max_length=LEN_TEXT)
    helped: Optional[bool] = None


class RecoverySchema(BaseModel):
    grade: str = Field(max_length=LEN_SHORT)
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    studentName: str = Field(min_length=1, max_length=LEN_NAME)
    attempts: int = Field(ge=0, le=100)
    previousApproaches: Optional[list[PreviousApproach]] = Field(default=None, max_length=20)


class PracticeQuizSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    interests: Optional[list[str]] = Field(default=None, max_length=20)


class FlashcardsSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    interests: Optional[list[str]] = Field(default=None, max_length=20)


class TestPrepSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    totalMarks: Optional[int] = Field(default=None, gt=0, le=1000)
    interests: Optional[list[str]] = Field(default=None, max_length=20)


class TestStudyGuideSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    totalMarks: Optional[int] = Field(default=None, gt=0, le=1000)
    interests: Optional[list[str]] = Field(default=None, max_length=20)


class ConceptGuideSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    interests: Optional[list[str]] = Field(default=None, max_length=20)


class StudentReportStudent(BaseModel):
    name: str = Field(max_length=LEN_NAME)
    rollNumber: str = Field(max_length=LEN_SHORT)
    interests: list[str] = Field(max_length=20)
    goal: str = Field(max_length=LEN_TEXT)


class StudentReportMark(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    score: float
    totalMarks: float = Field(gt=0)
    conductedOn: str = Field(max_length=LEN_SHORT)


class StudentReportMastery(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    mastery: float
    attempts: float


class StudentReportWarning(BaseModel):
    reason: str = Field(max_length=LEN_TEXT)
    action: str = Field(max_length=LEN_TEXT)
    level: str = Field(max_length=LEN_SHORT)


class StudentReportSchema(BaseModel):
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    attendanceRate: float = Field(ge=0, le=1)
    student: StudentReportStudent
    marks: list[StudentReportMark] = Field(max_length=500)
    mastery: list[StudentReportMastery] = Field(max_length=200)
    warnings: list[StudentReportWarning] = Field(max_length=50)


class ExtractSyllabusSchema(BaseModel):
    text: Optional[str] = Field(default=None, max_length=50_000)
    image: Optional[str] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _text_or_image(self):
        if not self.text and not self.image:
            raise ValueError("Provide text or image")
        return self


class ExtractStudentsSchema(BaseModel):
    image: str = Field(min_length=1)


class GradeImageStudent(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(max_length=LEN_NAME)


class GradeImageSchema(BaseModel):
    imageBase64: str = Field(min_length=1)
    students: list[GradeImageStudent] = Field(min_length=1, max_length=300)
    totalMarks: float = Field(gt=0, le=1000)
    topic: str = Field(max_length=LEN_TOPIC)


class AiQuestionSchema(BaseModel):
    text: str = Field(max_length=LEN_TEXT)
    type: Optional[Literal["mcq", "fill-in-blank", "short-answer", "long-answer"]] = None
    marks: float = Field(ge=0, le=200)
    difficulty: Optional[Literal["easy", "medium", "hard"]] = None
    options: Optional[list[str]] = Field(default=None, max_length=10)
    answer: Optional[str] = Field(default=None, max_length=LEN_TEXT)
    keywords: Optional[list[str]] = Field(default=None, max_length=30)


class GradePaperSchema(BaseModel):
    imageBase64: str = Field(min_length=1)
    questions: list[AiQuestionSchema] = Field(min_length=1, max_length=100)
    totalMarks: float = Field(gt=0, le=1000)
    topic: str = Field(max_length=LEN_TOPIC)
    studentName: str = Field(max_length=LEN_NAME)


class GradeScanStudent(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(max_length=LEN_NAME)
    rollNumber: int = Field(ge=0)


class GradeScanSchema(BaseModel):
    imageBase64: str = Field(min_length=1)
    mimeType: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    studentId: Optional[str] = Field(default=None, max_length=36)
    studentName: Optional[str] = Field(default=None, max_length=LEN_NAME)
    students: Optional[list[GradeScanStudent]] = Field(default=None, max_length=300)
    totalMarks: float = Field(gt=0, le=1000)
    topic: str = Field(max_length=LEN_TOPIC)
    subject: str = Field(max_length=LEN_TOPIC)
    questions: list[AiQuestionSchema] = Field(max_length=100)


class LessonPlanTopic(BaseModel):
    topic: str = Field(max_length=LEN_TOPIC)
    description: str = Field(max_length=LEN_TEXT)
    weekNumber: Optional[int] = Field(default=None, ge=0)
    isCompleted: bool


class LessonPlanSchema(BaseModel):
    topics: list[LessonPlanTopic] = Field(min_length=1, max_length=100)
    className: str = Field(min_length=1, max_length=LEN_NAME)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    studentInterests: list[str] = Field(max_length=20)


class ScanStudentsSchema(BaseModel):
    image: str = Field(min_length=1)


class ScanAttendanceStudent(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(max_length=LEN_NAME)
    rollNumber: str = Field(max_length=LEN_SHORT)


class ScanAttendanceSchema(BaseModel):
    imageBase64: str = Field(min_length=1)
    students: list[ScanAttendanceStudent] = Field(min_length=1, max_length=300)


class TestAnalysisResult(BaseModel):
    name: str = Field(max_length=LEN_NAME)
    score: float = Field(ge=0)
    percentage: float = Field(ge=0, le=100)


class TestAnalysisSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    totalMarks: float = Field(gt=0, le=1000)
    grade: str = Field(max_length=LEN_SHORT)
    subject: str = Field(max_length=LEN_TOPIC)
    results: list[TestAnalysisResult] = Field(min_length=1, max_length=300)


class SaveScoreSchema(BaseModel):
    studentId: str = Field(min_length=1, max_length=36)
    testId: str = Field(min_length=1, max_length=36)
    score: float = Field(ge=0)
    totalMarks: float = Field(gt=0, le=1000)
    source: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    feedback: Optional[str] = Field(default=None, max_length=LEN_TEXT)
    imageUrl: Optional[str] = Field(default=None, max_length=1000)
    driveUrl: Optional[str] = Field(default=None, max_length=1000)
    breakdown: Optional[list[dict]] = Field(default=None, max_length=100)


class UploadScanSchema(BaseModel):
    imageBase64: str = Field(min_length=1)
    mimeType: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    filename: Optional[str] = Field(default=None, max_length=LEN_NAME)


# ─── Manual-body routes (no zod schema in the original — loose shape checks) ─

class SmartLessonSchema(BaseModel):
    classId: str = Field(min_length=1, max_length=36)
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(min_length=1, max_length=LEN_TOPIC)
    grade: str = Field(min_length=1, max_length=LEN_SHORT)
    teacherId: Optional[str] = Field(default=None, max_length=36)
    subtopic: Optional[str] = Field(default=None, max_length=LEN_TOPIC)


class WsQuestion(BaseModel):
    text: str = Field(max_length=LEN_TEXT)
    options: Optional[list[str]] = None
    answer: Optional[str] = Field(default=None, max_length=LEN_TEXT)


class WsSection(BaseModel):
    type: str = Field(max_length=LEN_SHORT)
    label: str = Field(max_length=LEN_NAME)
    marksEach: float
    questions: list[WsQuestion]


class GenerateAnswerKeySchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    sections: list[WsSection] = Field(min_length=1)


class DistRow(BaseModel):
    type: str = Field(max_length=LEN_SHORT)
    count: int = Field(gt=0, le=100)
    marksEach: float = Field(gt=0)


class GenerateWorksheetSchema(BaseModel):
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: str = Field(max_length=LEN_TOPIC)
    grade: str = Field(max_length=LEN_SHORT)
    distribution: list[DistRow] = Field(min_length=1)


class WorksheetUpsertSchema(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    teacherId: str = Field(min_length=1, max_length=36)
    classId: Optional[str] = Field(default=None, max_length=36)
    topic: str = Field(min_length=1, max_length=LEN_TOPIC)
    subject: Optional[str] = Field(default=None, max_length=LEN_TOPIC)
    grade: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    template: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    totalMarks: Optional[float] = None
    sections: Optional[list] = None
    answerKey: Optional[dict] = None
    createdAt: Optional[str] = None


# ─── Vision / scanner routes (no zod schema in the original) ────────────────

class GradePaperSchema2(BaseModel):
    """grade-paper/route.ts parses the body with a manual TS cast, not a zod
    schema — this mirrors that same loose shape (AiQuestionSchema already
    covers the `questions` entries)."""
    imageBase64: str = Field(min_length=1)
    questions: list[AiQuestionSchema] = Field(min_length=1)
    totalMarks: float = Field(gt=0)
    topic: str = Field(max_length=LEN_TOPIC)
    studentName: str = Field(max_length=LEN_NAME)


class GradeScanStudent2(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(max_length=LEN_NAME)
    rollNumber: float


class GradeScanSchema2(BaseModel):
    imageBase64: str = Field(min_length=1)
    mimeType: Optional[str] = Field(default=None, max_length=LEN_SHORT)
    studentId: Optional[str] = Field(default=None, max_length=36)
    studentName: Optional[str] = Field(default=None, max_length=LEN_NAME)
    students: Optional[list[GradeScanStudent2]] = None
    totalMarks: float = Field(gt=0)
    topic: str = Field(max_length=LEN_TOPIC)
    subject: str = Field(max_length=LEN_TOPIC)
    questions: list[AiQuestionSchema] = Field(default_factory=list)


class MultiGradeScanSchema(BaseModel):
    images: list[str] = Field(min_length=1)
    studentId: str = Field(min_length=1, max_length=36)
    studentName: str = Field(max_length=LEN_NAME)
    totalMarks: float = Field(gt=0)
    topic: str = Field(max_length=LEN_TOPIC)
    subject: str = Field(max_length=LEN_TOPIC)
    questions: list[AiQuestionSchema] = Field(default_factory=list)
    testId: Optional[str] = Field(default=None, max_length=36)
    worksheetId: Optional[str] = Field(default=None, max_length=36)


class ScannerUploadSchema(BaseModel):
    testId: Optional[str] = Field(default=None, max_length=36)
    worksheetId: Optional[str] = Field(default=None, max_length=36)
    studentId: str = Field(min_length=1, max_length=36)
    imageDataUrl: str = Field(min_length=1)


class ScannerSaveScoreSchema(BaseModel):
    testId: str = Field(min_length=1, max_length=36)
    studentId: str = Field(min_length=1, max_length=36)
    score: float
    totalMarks: float = Field(gt=0)
    breakdown: Optional[list] = None
    feedback: Optional[str] = None
    imageUrl: Optional[str] = None
    driveUrl: Optional[str] = None


class WorksheetMarkEntry(BaseModel):
    studentId: str = Field(min_length=1, max_length=36)
    score: float
    feedback: Optional[str] = None
    source: Optional[str] = None


class WorksheetMarksUpsertSchema(BaseModel):
    worksheetId: str = Field(min_length=1, max_length=36)
    entries: list[WorksheetMarkEntry] = Field(min_length=1)


class WorksheetSaveScoreSchema(BaseModel):
    worksheetId: str = Field(min_length=1, max_length=36)
    studentId: str = Field(min_length=1, max_length=36)
    score: float
    totalMarks: float = Field(gt=0)
    breakdown: Optional[list] = None
    feedback: Optional[str] = None
    imageUrl: Optional[str] = None
    driveUrl: Optional[str] = None
