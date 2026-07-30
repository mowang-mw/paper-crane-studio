import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent,
} from "react";
import {
  API_BASE,
  ApiError,
  createProject,
  deleteProject,
  exportUrls,
  generateProject,
  getHealth,
  getJob,
  getProject,
  listProjects,
  retryJob,
} from "./api";
import type {
  GenerationJob,
  HealthStatus,
  JobStatus,
  Project,
  ProjectDetail,
} from "./types";

const PAPER_CRANE_STORY =
  "深夜，少女在窗边折出一只纸鹤。纸鹤亮起微光，飞过屋顶、灯火与云层；黎明时，它飞向远方，少女在窗边静静注视。";
const PAPER_CRANE_TITLE = "纸鹤的夜航";

type SectionName = "create" | "project" | "shots" | "result";
type Notice = { kind: "info" | "success"; message: string; action?: SectionName };

const statusLabels: Record<JobStatus, string> = {
  QUEUED: "等待 Worker",
  RUNNING: "生成中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
};

function readableError(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "发生未知错误，请检查后端日志。";
}

function displayHealth(health: HealthStatus | null): string {
  if (!health) return "连接中";
  if (typeof health.status === "string") return health.status;
  if (typeof health.service === "string") return health.service;
  return health.service?.status ?? "可用";
}

function projectStatus(project: Project): string {
  return project.workflow_status ?? project.status ?? "DRAFT";
}

function JobPanel({
  job,
  retrying,
  onRetry,
  onViewResult,
}: {
  job: GenerationJob;
  retrying: boolean;
  onRetry: () => void;
  onViewResult: () => void;
}) {
  const progress = Math.max(0, Math.min(100, Math.round(job.progress ?? 0)));
  return (
    <section className={`job-panel job-${job.status.toLowerCase()}`} aria-live="polite">
      <div className="job-heading">
        <div>
          <span className="eyebrow">生成任务</span>
          <h3>{statusLabels[job.status] ?? job.status}</h3>
        </div>
        <span className="job-percent">{progress}%</span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <p className="job-meta">
        任务 {job.id.slice(0, 8)} · Provider：{job.provider_id ?? "mock"}
      </p>
      {job.status === "QUEUED" && (
        <p className="job-hint">任务已入队。请确认独立 Worker 正在运行。</p>
      )}
      {job.status === "FAILED" && (
        <div className="failure-box">
          <p>{job.error_message || "生成失败，后端没有返回详细信息。"}</p>
          <button className="button button-danger" onClick={onRetry} disabled={retrying}>
            {retrying ? "正在创建重试任务…" : "手动重试"}
          </button>
        </div>
      )}
      {job.status === "SUCCEEDED" && (
        <div className="success-box">
          <p>短片已经生成完成，可在下方播放和下载。</p>
          <button className="button button-success" type="button" onClick={onViewResult}>
            查看成片
          </button>
        </div>
      )}
    </section>
  );
}

function StageNavigation({
  current,
  completed,
  available,
  onNavigate,
}: {
  current: SectionName;
  completed: Record<SectionName, boolean>;
  available: Record<SectionName, boolean>;
  onNavigate: (section: SectionName) => void;
}) {
  const stages: Array<{ key: SectionName; number: string; label: string }> = [
    { key: "create", number: "01", label: "创建项目" },
    { key: "project", number: "02", label: "生成任务" },
    { key: "shots", number: "03", label: "查看镜头" },
    { key: "result", number: "04", label: "播放与下载成片" },
  ];
  return (
    <nav className="stage-navigation" aria-label="制作流程">
      {stages.map((stage) => {
        const state = current === stage.key ? "current" : completed[stage.key] ? "done" : "pending";
        return (
          <button
            key={stage.key}
            className={`stage-step is-${state}`}
            type="button"
            disabled={!available[stage.key]}
            aria-current={current === stage.key ? "step" : undefined}
            onClick={() => onNavigate(stage.key)}
          >
            <span className="stage-marker">{completed[stage.key] ? "✓" : stage.number}</span>
            <span>{stage.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [activeJob, setActiveJob] = useState<GenerationJob | null>(null);
  const [title, setTitle] = useState("");
  const [story, setStory] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<Project | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const createSectionRef = useRef<HTMLElement>(null);
  const projectSectionRef = useRef<HTMLElement>(null);
  const shotsSectionRef = useRef<HTMLElement>(null);
  const resultSectionRef = useRef<HTMLElement>(null);
  const projectTitleRef = useRef<HTMLHeadingElement>(null);
  const resultTitleRef = useRef<HTMLHeadingElement>(null);
  const creationInFlightRef = useRef(false);
  const deletionInFlightRef = useRef(false);
  const pendingNavigationRef = useRef<"project" | "result" | null>(null);
  const handledSucceededJobsRef = useRef(new Set<string>());

  const scrollToSection = useCallback((section: SectionName) => {
    const sections: Record<SectionName, HTMLElement | null> = {
      create: createSectionRef.current,
      project: projectSectionRef.current,
      shots: shotsSectionRef.current,
      result: resultSectionRef.current,
    };
    const target = sections[section];
    if (!target) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    const focusTarget =
      section === "project"
        ? projectTitleRef.current ?? target
        : section === "result"
          ? resultTitleRef.current ?? target
          : target;
    focusTarget.focus({ preventScroll: true });
  }, []);

  const refreshProjects = useCallback(async () => {
    const items = await listProjects();
    setProjects(items);
    return items;
  }, []);

  const refreshDetail = useCallback(async (projectId: string) => {
    const value = await getProject(projectId);
    setDetail(value);
    const pending = value.recent_jobs.find(
      (job) => job.status === "QUEUED" || job.status === "RUNNING",
    );
    if (pending) setActiveJob(pending);
    return value;
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getHealth(), refreshProjects()])
      .then(([healthValue, items]) => {
        if (cancelled) return;
        setHealth(healthValue);
        if (items.length > 0) setSelectedId((current) => current ?? items[0].id);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setHealthError(readableError(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshProjects]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setActiveJob(null);
    setError("");
    refreshDetail(selectedId).catch((cause: unknown) => setError(readableError(cause)));
  }, [refreshDetail, selectedId]);

  useEffect(() => {
    if (!activeJob || (activeJob.status !== "QUEUED" && activeJob.status !== "RUNNING")) return;

    let cancelled = false;
    const timer = window.setInterval(() => {
      getJob(activeJob.id)
        .then(async (job) => {
          if (cancelled) return;
          if (
            job.status === "SUCCEEDED" &&
            activeJob.status !== "SUCCEEDED" &&
            !handledSucceededJobsRef.current.has(job.id)
          ) {
            handledSucceededJobsRef.current.add(job.id);
            pendingNavigationRef.current = "result";
            setNotice({
              kind: "success",
              message: "短片已经生成完成，可在下方播放和下载。",
              action: "result",
            });
          }
          setActiveJob(job);
          if (job.status === "SUCCEEDED" || job.status === "FAILED") {
            window.clearInterval(timer);
            await Promise.all([refreshDetail(job.project_id), refreshProjects()]);
          }
        })
        .catch((cause: unknown) => {
          if (!cancelled) setError(`任务状态更新失败：${readableError(cause)}`);
        });
    }, 1200);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeJob, refreshDetail, refreshProjects]);

  useEffect(() => {
    if (
      pendingNavigationRef.current === "project" &&
      selectedId &&
      detail?.project.id === selectedId
    ) {
      pendingNavigationRef.current = null;
      scrollToSection("project");
    }
  }, [detail?.project.id, scrollToSection, selectedId]);

  useEffect(() => {
    if (pendingNavigationRef.current !== "result" || !detail?.latest_export) return;
    pendingNavigationRef.current = null;
    const focused = document.activeElement;
    const userIsEditing =
      focused instanceof HTMLInputElement ||
      focused instanceof HTMLTextAreaElement ||
      focused instanceof HTMLSelectElement ||
      (focused instanceof HTMLElement && focused.isContentEditable);
    if (!userIsEditing) scrollToSection("result");
  }, [detail?.latest_export, scrollToSection]);

  const submitProject = async (event: FormEvent) => {
    event.preventDefault();
    if (
      creationInFlightRef.current ||
      busy !== null ||
      !title.trim() ||
      !story.trim()
    ) {
      return;
    }
    creationInFlightRef.current = true;
    setBusy("create");
    setError("");
    setNotice(null);
    try {
      const project = await createProject({ title: title.trim(), story: story.trim() });
      await refreshProjects();
      pendingNavigationRef.current = "project";
      setSelectedId(project.id);
      setNotice({
        kind: "success",
        message: `项目“${project.title}”已创建并选中。表单内容已保留，可继续修改。`,
      });
    } catch (cause) {
      setError(`创建项目失败：${readableError(cause)}`);
    } finally {
      creationInFlightRef.current = false;
      setBusy(null);
    }
  };

  const makeDemo = () => {
    if (creationInFlightRef.current || busy !== null) return;
    setError("");
    setTitle(PAPER_CRANE_TITLE);
    setStory(PAPER_CRANE_STORY);
    setNotice({
      kind: "info",
      message: "演示故事已填入，请确认内容后点击创建项目。",
    });
  };

  const openDeleteConfirmation = (event: MouseEvent, project: Project) => {
    event.stopPropagation();
    if (deletingId) return;
    setDeleteCandidate(project);
    setError("");
  };

  const confirmDelete = async (event: MouseEvent) => {
    event.stopPropagation();
    if (!deleteCandidate || deletionInFlightRef.current) return;
    deletionInFlightRef.current = true;
    const project = deleteCandidate;
    setDeletingId(project.id);
    setError("");
    setNotice(null);
    try {
      await deleteProject(project.id);
      const items = await refreshProjects();
      setDeleteCandidate(null);
      if (selectedId === project.id) {
        setDetail(null);
        setActiveJob(null);
        setSelectedId(items[0]?.id ?? null);
      }
      setNotice({ kind: "success", message: `项目“${project.title}”已删除。` });
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setError("当前项目仍有任务正在等待或生成，请等待任务结束后再删除。");
      } else {
        setError(`删除项目失败：${readableError(cause)}`);
      }
    } finally {
      deletionInFlightRef.current = false;
      setDeletingId(null);
    }
  };

  const startGeneration = async () => {
    if (!selectedId) return;
    setBusy("generate");
    setError("");
    setNotice(null);
    try {
      const job = await generateProject(selectedId);
      setActiveJob({ ...job, project_id: job.project_id || selectedId });
      await refreshDetail(selectedId);
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const retryGeneration = async (failedJob: GenerationJob) => {
    setBusy("retry");
    setError("");
    setNotice(null);
    try {
      const job = await retryJob(failedJob.id);
      setActiveJob({ ...job, project_id: job.project_id || failedJob.project_id });
      await refreshDetail(failedJob.project_id);
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const selectedProject = detail?.project ?? projects.find((item) => item.id === selectedId) ?? null;
  const latestVisibleJob = activeJob ?? detail?.recent_jobs[0] ?? null;
  const media = useMemo(() => {
    if (!selectedId || !detail?.latest_export) return null;
    return exportUrls(selectedId, detail.latest_export);
  }, [detail?.latest_export, selectedId]);
  const generationInProgress =
    activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING";
  const currentStage: SectionName =
    latestVisibleJob?.status === "QUEUED" ||
    latestVisibleJob?.status === "RUNNING" ||
    latestVisibleJob?.status === "FAILED"
      ? "project"
      : media
        ? "result"
        : detail?.shots.length
          ? "shots"
          : selectedProject
            ? "project"
            : "create";
  const completedStages: Record<SectionName, boolean> = {
    create: Boolean(selectedProject),
    project: latestVisibleJob?.status === "SUCCEEDED" || Boolean(media),
    shots: Boolean(detail?.shots.length),
    result: Boolean(media),
  };
  const availableStages: Record<SectionName, boolean> = {
    create: true,
    project: true,
    shots: Boolean(detail?.shots.length),
    result: Boolean(media),
  };

  return (
    <div className="app-shell">
      <header className="hero">
        <nav className="topbar" aria-label="主导航">
          <a className="brand" href="#top" aria-label="纸鹤工坊首页">
            <span className="brand-mark">折</span>
            <span>
              <strong>纸鹤工坊</strong>
              <small>Paper Crane Studio</small>
            </span>
          </a>
          <div className={`health-pill ${healthError ? "is-offline" : ""}`}>
            <span className="health-dot" />
            {healthError ? "后端未连接" : `M2 · ${displayHealth(health)}`}
          </div>
        </nav>

        <div className="hero-copy" id="top">
          <p className="kicker">MOCK PROVIDER × FFMPEG</p>
          <h1>把一个故事，折成一段<br />真正可播放的短片。</h1>
          <p>
            当前纵向链路使用确定性 Mock 素材。项目、任务、镜头与成片均由后端持久化，
            Worker 在 HTTP 请求之外完成渲染。
          </p>
          <a className="text-link" href="#workspace">开始创建 <span>↓</span></a>
        </div>
        <div className="hero-orbit" aria-hidden="true">
          <span className="crane">⌁</span>
          <i className="star star-one" />
          <i className="star star-two" />
          <i className="star star-three" />
        </div>
      </header>

      <main id="workspace">
        {(error || healthError) && (
          <div className="global-alert" role="alert">
            <strong>{healthError ? "连接提示" : "操作失败"}</strong>
            <span>{error || healthError}</span>
            <small>API：{API_BASE}</small>
          </div>
        )}

        {notice && (
          <div className={`inline-notice notice-${notice.kind}`} aria-live="polite">
            <span>{notice.message}</span>
            {notice.action && availableStages[notice.action] && (
              <button className="notice-action" type="button" onClick={() => scrollToSection(notice.action!)}>
                查看成片
              </button>
            )}
          </div>
        )}

        <section
          className="section create-section"
          id="create-section"
          ref={createSectionRef}
          tabIndex={-1}
        >
          <div className="section-heading">
            <span className="section-number">01</span>
            <div>
              <p className="eyebrow">创建项目</p>
              <h2>从短篇故事开始</h2>
            </div>
          </div>
          <div className="create-grid">
            <form className="story-form" onSubmit={submitProject}>
              <label>
                项目标题
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  maxLength={120}
                  required
                  placeholder="例如：纸鹤的夜航"
                />
              </label>
              <label>
                故事梗概
                <textarea
                  value={story}
                  onChange={(event) => setStory(event.target.value)}
                  maxLength={4000}
                  rows={6}
                  required
                  placeholder="写下一个可以拆成四个镜头的短故事……"
                />
              </label>
              <div className="form-actions">
                <button className="button button-primary" type="submit" disabled={busy !== null}>
                  {busy === "create" ? "正在创建……" : "创建项目"}
                </button>
                <button
                  className="button button-ghost"
                  type="button"
                  onClick={makeDemo}
                  disabled={busy !== null}
                >
                  载入《纸鹤的夜航》Demo
                </button>
              </div>
            </form>
            <aside className="workflow-card">
              <p className="eyebrow">本次生成路径</p>
              <ol>
                <li><span>1</span>Mock 剧本拆成 4 个镜头</li>
                <li><span>2</span>Worker 领取 SQLite 任务</li>
                <li><span>3</span>FFmpeg 合成画面、字幕与音频</li>
                <li><span>4</span>浏览器播放并下载 MP4</li>
              </ol>
              <p className="offline-note">无需网络、API Key 或模型权重</p>
            </aside>
          </div>
        </section>

        <StageNavigation
          current={currentStage}
          completed={completedStages}
          available={availableStages}
          onNavigate={scrollToSection}
        />

        <section
          className="section projects-section"
          id="project-section"
          ref={projectSectionRef}
          tabIndex={-1}
        >
          <div className="section-heading">
            <span className="section-number">02</span>
            <div>
              <p className="eyebrow">项目与任务</p>
              <h2>选择一个项目生成</h2>
            </div>
          </div>
          <div className="projects-layout">
            <aside className="project-list" aria-label="项目列表">
              {projects.length === 0 ? (
                <div className="empty-state">还没有项目，请先创建一个故事。</div>
              ) : (
                projects.map((project) => (
                  <div className="project-list-entry" key={project.id}>
                    <div className={`project-item ${selectedId === project.id ? "is-active" : ""}`}>
                      <button
                        type="button"
                        className="project-select"
                        disabled={deletingId !== null}
                        onClick={() => {
                          setDeleteCandidate(null);
                          setSelectedId(project.id);
                          setActiveJob(null);
                        }}
                      >
                        <span className="project-index">{project.title.slice(0, 1)}</span>
                        <span className="project-summary">
                          <strong>{project.title}</strong>
                          <small>{projectStatus(project)}</small>
                        </span>
                        <span aria-hidden="true">›</span>
                      </button>
                      <button
                        className="project-delete"
                        type="button"
                        aria-label={`删除项目“${project.title}”`}
                        disabled={deletingId !== null}
                        onClick={(event) => openDeleteConfirmation(event, project)}
                      >
                        删除
                      </button>
                    </div>
                    {deleteCandidate?.id === project.id && (
                      <div
                        className="delete-confirmation"
                        role="dialog"
                        aria-modal="false"
                        aria-labelledby={`delete-title-${project.id}`}
                      >
                        <strong id={`delete-title-${project.id}`}>确认删除“{project.title}”？</strong>
                        <p>删除后项目、任务和生成文件将无法恢复。</p>
                        <div className="delete-actions">
                          <button
                            className="button button-ghost button-small"
                            type="button"
                            disabled={deletingId !== null}
                            onClick={(event) => {
                              event.stopPropagation();
                              setDeleteCandidate(null);
                            }}
                          >
                            取消
                          </button>
                          <button
                            className="button button-danger button-small"
                            type="button"
                            disabled={deletingId !== null}
                            onClick={confirmDelete}
                          >
                            {deletingId === project.id ? "正在删除……" : "确认删除"}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                ))
              )}
            </aside>

            <article className="project-detail">
              {!selectedProject ? (
                <div className="empty-state large">选择项目后，这里会显示镜头与生成状态。</div>
              ) : (
                <>
                  <div className="project-title-row">
                    <div>
                      <p className="eyebrow">{projectStatus(selectedProject)}</p>
                      <h3 ref={projectTitleRef} tabIndex={-1}>{selectedProject.title}</h3>
                    </div>
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={startGeneration}
                      disabled={busy !== null || generationInProgress}
                    >
                      {busy === "generate"
                        ? "正在提交…"
                        : generationInProgress
                          ? "任务进行中"
                          : detail?.latest_export
                            ? "再次生成"
                            : "生成 Mock 短片"}
                    </button>
                  </div>
                  <p className="story-preview">{selectedProject.story}</p>
                  {detail && detail.shots.length === 0 && !latestVisibleJob && (
                    <p className="inline-empty">
                      尚未生成分镜。提交任务后，独立 Worker 会写入 4 个结构化镜头。
                    </p>
                  )}
                  {latestVisibleJob && (
                    <JobPanel
                      job={latestVisibleJob}
                      retrying={busy === "retry"}
                      onRetry={() => retryGeneration(latestVisibleJob)}
                      onViewResult={() => scrollToSection("result")}
                    />
                  )}
                </>
              )}
            </article>
          </div>
        </section>

        {detail && detail.shots.length > 0 && (
          <section
            className="section shots-section"
            id="shots-section"
            ref={shotsSectionRef}
            tabIndex={-1}
          >
            <div className="section-heading compact">
              <span className="section-number">03</span>
              <div>
                <p className="eyebrow">结构化分镜</p>
                <h2>{detail.shots.length} 个镜头</h2>
              </div>
            </div>
            <div className="shot-grid">
              {[...detail.shots]
                .sort((left, right) => left.shot_index - right.shot_index)
                .map((shot, index) => (
                  <article className="shot-card" key={shot.id}>
                    <div className="shot-art" data-shot={(index % 4) + 1}>
                      <span>{String(index + 1).padStart(2, "0")}</span>
                      <i />
                    </div>
                    <div className="shot-copy">
                      <p className="eyebrow">{shot.duration_seconds}s · {shot.provider_id}</p>
                      <h3>{shot.title}</h3>
                      <p>{shot.visual_description}</p>
                      <blockquote>“{shot.narration}”</blockquote>
                    </div>
                  </article>
                ))}
            </div>
            <div
              className={`shot-next-step next-${latestVisibleJob?.status?.toLowerCase() ?? "ready"}`}
              aria-live="polite"
            >
              {latestVisibleJob?.status === "QUEUED" || latestVisibleJob?.status === "RUNNING" ? (
                <p>镜头和成片正在生成，请查看任务进度。</p>
              ) : latestVisibleJob?.status === "FAILED" ? (
                <>
                  <p>生成失败，请返回任务区域查看错误并重试。</p>
                  <button className="button button-ghost" type="button" onClick={() => scrollToSection("project")}>
                    返回任务区域
                  </button>
                </>
              ) : media ? (
                <>
                  <p>镜头已准备完成，最终成片已生成。</p>
                  <button className="button button-primary" type="button" onClick={() => scrollToSection("result")}>
                    前往播放成片 ↓
                  </button>
                </>
              ) : (
                <p>镜头已准备完成，正在等待最终成片。</p>
              )}
            </div>
          </section>
        )}

        {detail?.latest_export && media && (
          <section
            className="section result-section is-ready"
            id="result-section"
            ref={resultSectionRef}
            tabIndex={-1}
          >
            <div className="section-heading light compact">
              <span className="section-number">04</span>
              <div>
                <p className="eyebrow">播放与下载</p>
                <h2 ref={resultTitleRef} tabIndex={-1}>最终成片</h2>
              </div>
            </div>
            <div className="result-status-summary" aria-label="成片状态">
              <span className="result-ready">● 已生成</span>
              <span>{detail.latest_export.duration_seconds?.toFixed(2) ?? "—"} 秒</span>
              <span>{detail.shots.length} 个镜头</span>
            </div>
            <div className="result-grid">
              <div className="video-frame">
                <video controls preload="metadata" src={media.video}>
                  当前浏览器不支持 HTML5 视频，请下载 MP4 后播放。
                </video>
              </div>
              <aside className="export-info">
                <p className="eyebrow">EXPORT READY</p>
                <h3>{detail.project.title}</h3>
                <dl>
                  <div><dt>时长</dt><dd>{detail.latest_export.duration_seconds?.toFixed(2) ?? "—"} 秒</dd></div>
                  <div><dt>镜头</dt><dd>{detail.shots.length} 个</dd></div>
                  <div><dt>Provider</dt><dd>mock</dd></div>
                  <div>
                    <dt>SHA-256</dt>
                    <dd title={detail.latest_export.sha256}>{detail.latest_export.sha256?.slice(0, 12) ?? "—"}…</dd>
                  </div>
                </dl>
                <div className="download-actions">
                  <a className="button button-light" href={media.download} download>
                    下载 MP4
                  </a>
                  <a className="button button-outline-light" href={media.manifest} download>
                    下载 Manifest
                  </a>
                </div>
              </aside>
            </div>
          </section>
        )}
      </main>

      {latestVisibleJob && (
        <aside className={`task-shortcut shortcut-${latestVisibleJob.status.toLowerCase()}`} aria-live="polite">
          {latestVisibleJob.status === "QUEUED" || latestVisibleJob.status === "RUNNING" ? (
            <span>正在生成短片 · {Math.max(0, Math.min(100, Math.round(latestVisibleJob.progress)))}%</span>
          ) : latestVisibleJob.status === "SUCCEEDED" && media ? (
            <>
              <span>短片已生成</span>
              <button type="button" onClick={() => scrollToSection("result")}>查看成片</button>
            </>
          ) : latestVisibleJob.status === "FAILED" ? (
            <>
              <span>短片生成失败</span>
              <button type="button" onClick={() => scrollToSection("project")}>查看任务</button>
            </>
          ) : null}
        </aside>
      )}

      <footer>
        <span>纸鹤工坊 · M2 最小全栈纵向链路</span>
        <span>Mock 可运行，Provider 可替换</span>
      </footer>
    </div>
  );
}
