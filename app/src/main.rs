//! Verbatim: a native Omarchy window for the local meeting recorder.
//!
//! The app is a thin client over the Python backend's JSON API (see
//! `api.rs`); it keeps no data of its own. `ui.rs` draws the window.

mod api;
mod theme;
mod ui;

#[cfg(test)]
mod e2e;

use api::{Api, JobStatus, MeetingDetail, MeetingSummary, Status, Utterances};
use iced::widget::{markdown, operation};
use iced::{Size, Subscription, Task, event, keyboard, time, window};
use serde_json::{Value, json};
use std::time::Duration;
use theme::OmarchyTheme;

pub const SEARCH_ID: &str = "search";
pub const APP_ID: &str = "verbatim";

/// Startup options: `verbatim-app [--select ID] [--tab summary|transcript]
/// [--search TEXT]`, so a launcher or agent can open a given meeting.
#[derive(Debug, Clone, Default)]
struct Options {
    select: Option<String>,
    tab: Option<Tab>,
    search: Option<String>,
}

fn options() -> Options {
    let mut o = Options::default();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--select" => o.select = args.next(),
            "--search" => o.search = args.next(),
            "--tab" => {
                o.tab = match args.next().as_deref() {
                    Some("transcript") => Some(Tab::Transcript),
                    Some("summary") => Some(Tab::Summary),
                    _ => None,
                }
            }
            "-h" | "--help" => {
                println!("usage: verbatim-app [--select MEETING_ID] [--tab summary|transcript] [--search TEXT]");
                println!("Set VERBATIM_URL to use a backend other than {}.", api::DEFAULT_URL);
                std::process::exit(0);
            }
            other => {
                eprintln!("verbatim-app: unknown argument {other} (try --help)");
                std::process::exit(2);
            }
        }
    }
    o
}

fn main() -> iced::Result {
    let opts = options();
    let boot_theme = OmarchyTheme::load();
    let default_font = boot_theme.font;
    iced::application(move || App::new(boot_theme.clone(), opts.clone()), App::update, App::view)
        .title("Verbatim")
        .theme(|app: &App| app.theme.iced_theme())
        .subscription(App::subscription)
        .default_font(default_font)
        .window(window::Settings {
            size: Size::new(1400.0, 900.0),
            min_size: Some(Size::new(900.0, 560.0)),
            platform_specific: window::settings::PlatformSpecific {
                application_id: APP_ID.to_string(),
                ..Default::default()
            },
            ..Default::default()
        })
        .run()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tab {
    Summary,
    Transcript,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobKind {
    Summary,
    Identify,
}

#[derive(Debug, Clone)]
pub struct Job {
    pub meeting_id: String,
    pub running: bool,
    pub error: Option<String>,
}

/// Consecutive transcript lines by the same speaker.
#[derive(Debug, Clone)]
pub struct Block {
    pub speaker: String,
    /// Indexes into `Utterances::utterances`.
    pub lines: Vec<usize>,
    pub ai: bool,
    /// The acoustic tag ("Speaker 1") when the block shows a real name.
    pub tag: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockEditMode {
    NewName,
    RenameEverywhere,
}

#[derive(Debug, Clone)]
pub struct BlockEdit {
    pub block: usize,
    pub mode: BlockEditMode,
    pub value: String,
}

#[derive(Debug, Clone)]
pub struct Notice {
    pub text: String,
    pub error: bool,
}

pub struct App {
    pub api: Api,
    pub theme: OmarchyTheme,

    pub status: Status,
    pub status_error: Option<String>,
    status_in_flight: bool,
    service_start_tried: bool,
    pub record_busy: bool,
    pub title_input: String,

    pub search: String,
    search_seq: u64,
    pub meetings: Vec<MeetingSummary>,
    pub list_error: Option<String>,

    pub selected: Option<String>,
    pub detail: Option<MeetingDetail>,
    pub detail_error: Option<String>,
    pub summary: Vec<markdown::Item>,
    pub utterances: Option<Utterances>,
    pub blocks: Vec<Block>,
    pub tab: Tab,

    pub title_edit: String,
    pub label_edits: Vec<(i64, String, String)>,
    pub show_names: bool,
    pub show_identify: bool,
    pub who: String,
    pub confirm_delete: bool,
    pub block_edit: Option<BlockEdit>,
    pub summary_job: Option<Job>,
    pub identify_job: Option<Job>,
    pub busy: Option<String>,
    pub notice: Option<Notice>,
}

#[derive(Debug, Clone)]
pub enum Message {
    Tick,
    StatusLoaded(Result<Status, String>),
    ServiceStarted,
    TitleInput(String),
    RecordPressed,
    RecordDone(Result<Value, String>),

    SearchInput(String),
    SearchFire(u64),
    ListLoaded(u64, Result<Vec<MeetingSummary>, String>),
    FocusSearch,
    Escape,

    Select(String),
    DetailLoaded(String, Result<MeetingDetail, String>),
    UtterancesLoaded(String, Result<Utterances, String>),
    ShowTab(Tab),

    EditTitle(String),
    SaveTitle,
    ToggleNames,
    EditLabel(usize, String),
    SaveLabels,
    Summarise,
    ToggleIdentify,
    WhoInput(String),
    Identify,
    JobStarted(JobKind, String, Result<Value, String>),
    PollJobs,
    JobPolled(JobKind, String, Result<JobStatus, String>),
    OpenNote,
    NoteWritten(Result<Value, String>),
    OpenAudio,
    CopySummary,
    AskDelete,
    CancelDelete,
    ConfirmDelete,
    Deleted(String, Result<Value, String>),

    BlockPick(usize, String),
    BlockEditInput(String),
    BlockEditSave,
    BlockEditCancel,
    /// A change to a meeting finished; reload it and the list.
    Changed(String, Result<Value, String>),
    Link(markdown::Uri),
}

pub const NEW_NAME: &str = "New name…";
pub const RENAME_ALL: &str = "Rename everywhere…";

impl App {
    fn new(theme: OmarchyTheme, opts: Options) -> (Self, Task<Message>) {
        let mut app = Self {
            api: Api::from_env(),
            theme,
            status: Status { state: "idle".into(), ..Default::default() },
            status_error: None,
            status_in_flight: true,
            service_start_tried: false,
            record_busy: false,
            title_input: String::new(),
            search: String::new(),
            search_seq: 0,
            meetings: Vec::new(),
            list_error: None,
            selected: None,
            detail: None,
            detail_error: None,
            summary: Vec::new(),
            utterances: None,
            blocks: Vec::new(),
            tab: Tab::Summary,
            title_edit: String::new(),
            label_edits: Vec::new(),
            show_names: false,
            show_identify: false,
            who: String::new(),
            confirm_delete: false,
            block_edit: None,
            summary_job: None,
            identify_job: None,
            busy: None,
            notice: None,
        };
        if let Some(tab) = opts.tab {
            app.tab = tab;
        }
        if let Some(q) = opts.search {
            app.search = q;
        }
        let mut tasks = vec![app.fetch_status(), app.fetch_list()];
        if let Some(id) = opts.select {
            tasks.push(app.update(Message::Select(id)));
        }
        (app, Task::batch(tasks))
    }

    fn subscription(&self) -> Subscription<Message> {
        let mut subs = vec![
            time::every(Duration::from_secs(1)).map(|_| Message::Tick),
            event::listen_with(shortcut),
        ];
        let running = |j: &Option<Job>| j.as_ref().is_some_and(|j| j.running);
        if running(&self.summary_job) || running(&self.identify_job) {
            subs.push(time::every(Duration::from_secs(3)).map(|_| Message::PollJobs));
        }
        Subscription::batch(subs)
    }

    fn fetch_status(&self) -> Task<Message> {
        let api = self.api.clone();
        Task::perform(api::run(move || api.status()), Message::StatusLoaded)
    }

    fn fetch_list(&self) -> Task<Message> {
        let api = self.api.clone();
        let seq = self.search_seq;
        let query = self.search.trim().to_string();
        Task::perform(
            api::run(move || if query.is_empty() { api.meetings() } else { api.search(&query) }),
            move |r| Message::ListLoaded(seq, r),
        )
    }

    fn fetch_detail(&self, id: &str) -> Task<Message> {
        let (a, b) = (self.api.clone(), self.api.clone());
        let (i1, i2) = (id.to_string(), id.to_string());
        Task::batch([
            Task::perform(api::run(move || a.meeting(&i1)), {
                let id = id.to_string();
                move |r| Message::DetailLoaded(id.clone(), r)
            }),
            Task::perform(api::run(move || b.utterances(&i2)), {
                let id = id.to_string();
                move |r| Message::UtterancesLoaded(id.clone(), r)
            }),
        ])
    }

    fn poll_job(&self, kind: JobKind, id: &str) -> Task<Message> {
        let api = self.api.clone();
        let mid = id.to_string();
        let id = id.to_string();
        Task::perform(
            api::run(move || match kind {
                JobKind::Summary => api.analysis_status(&mid),
                JobKind::Identify => api.identify_status(&mid),
            }),
            move |r| Message::JobPolled(kind, id.clone(), r),
        )
    }

    /// Posts an action for the selected meeting and reloads it afterwards.
    fn change(&self, id: &str, action: &'static str, body: Value) -> Task<Message> {
        let api = self.api.clone();
        let mid = id.to_string();
        let id = id.to_string();
        Task::perform(api::run(move || api.action(&mid, action, body)), move |r| {
            Message::Changed(id.clone(), r)
        })
    }

    fn notice(&mut self, text: impl Into<String>, error: bool) {
        self.notice = Some(Notice { text: text.into(), error });
    }

    /// Whether `id` (full or short) is the selected meeting.
    pub fn is_selected(&self, id: &str) -> bool {
        self.selected.as_deref().is_some_and(|s| same_id(s, id))
    }

    pub fn recording(&self) -> bool {
        self.status.state == "recording"
    }

    pub fn processing(&self) -> bool {
        self.status.state == "processing"
    }

    fn toggle_record(&mut self) -> Task<Message> {
        if self.record_busy || self.processing() || self.status_error.is_some() {
            return Task::none();
        }
        self.record_busy = true;
        let api = self.api.clone();
        if self.recording() {
            Task::perform(api::run(move || api.stop()), Message::RecordDone)
        } else {
            let title = self.title_input.trim().to_string();
            Task::perform(api::run(move || api.start(&title)), Message::RecordDone)
        }
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Tick => {
                self.theme.refresh();
                if self.status_in_flight {
                    return Task::none();
                }
                self.status_in_flight = true;
                self.fetch_status()
            }
            Message::StatusLoaded(result) => {
                self.status_in_flight = false;
                match result {
                    Ok(status) => {
                        let was_processing = self.processing();
                        let reconnected = self.status_error.take().is_some();
                        let finished = was_processing && status.state != "processing";
                        let new_meeting = status.meeting_id.clone();
                        self.status = status;
                        let mut tasks = Vec::new();
                        if reconnected {
                            tasks.push(self.fetch_list());
                        }
                        if finished {
                            tasks.push(self.fetch_list());
                            if self.status.state == "idle"
                                && let Some(id) = new_meeting {
                                    tasks.push(self.update(Message::Select(id)));
                                }
                        }
                        Task::batch(tasks)
                    }
                    Err(e) => {
                        self.status_error = Some(e);
                        // The backend runs as a user service; start it once if
                        // it is not answering (never for a custom URL).
                        if self.api.is_default() && !self.service_start_tried {
                            self.service_start_tried = true;
                            return Task::perform(
                                api::run(|| {
                                    let _ = std::process::Command::new("systemctl")
                                        .args(["--user", "start", "verbatim.service"])
                                        .status();
                                }),
                                |_| Message::ServiceStarted,
                            );
                        }
                        Task::none()
                    }
                }
            }
            Message::ServiceStarted => Task::none(),
            Message::TitleInput(s) => {
                self.title_input = s;
                Task::none()
            }
            Message::RecordPressed => self.toggle_record(),
            Message::RecordDone(result) => {
                self.record_busy = false;
                match result {
                    Ok(_) => self.title_input.clear(),
                    Err(e) => self.notice(e, true),
                }
                self.status_in_flight = true;
                self.fetch_status()
            }

            Message::SearchInput(s) => {
                self.search = s;
                self.search_seq += 1;
                let seq = self.search_seq;
                // Searching reads every transcript, so wait for a pause in typing.
                Task::perform(tokio::time::sleep(Duration::from_millis(250)), move |_| {
                    Message::SearchFire(seq)
                })
            }
            Message::SearchFire(seq) => {
                if seq != self.search_seq {
                    return Task::none();
                }
                self.fetch_list()
            }
            Message::ListLoaded(seq, result) => {
                if seq != self.search_seq {
                    return Task::none();
                }
                match result {
                    Ok(list) => {
                        self.meetings = list;
                        self.list_error = None;
                    }
                    Err(e) => self.list_error = Some(e),
                }
                Task::none()
            }
            Message::FocusSearch => operation::focus(SEARCH_ID),
            Message::Escape => {
                if self.block_edit.take().is_some() || std::mem::take(&mut self.confirm_delete) {
                    return Task::none();
                }
                if self.show_identify {
                    self.show_identify = false;
                    return Task::none();
                }
                if self.search.is_empty() {
                    return Task::none();
                }
                self.search.clear();
                self.search_seq += 1;
                self.fetch_list()
            }

            Message::Select(id) => {
                if !self.is_selected(&id) {
                    self.detail = None;
                    self.utterances = None;
                    self.blocks.clear();
                    self.summary.clear();
                    self.show_names = false;
                    self.show_identify = false;
                    self.confirm_delete = false;
                    self.block_edit = None;
                    self.notice = None;
                }
                self.detail_error = None;
                self.selected = Some(id.clone());
                Task::batch([
                    self.fetch_detail(&id),
                    // Pick up summary or identify jobs started earlier.
                    self.poll_job(JobKind::Summary, &id),
                    self.poll_job(JobKind::Identify, &id),
                ])
            }
            Message::DetailLoaded(id, result) => {
                if !self.is_selected(&id) {
                    return Task::none();
                }
                match result {
                    Ok(detail) => {
                        // A short id from --select or the status file becomes the full one.
                        self.selected = Some(detail.id.clone());
                        self.summary = markdown::parse(&detail.analysis).collect();
                        self.title_edit = detail.title.clone();
                        self.label_edits = detail
                            .speaker_slots
                            .iter()
                            .filter_map(|s| s.num.map(|n| (n, s.raw.clone(), s.label.clone())))
                            .collect();
                        self.detail = Some(detail);
                        self.detail_error = None;
                    }
                    Err(e) => self.detail_error = Some(e),
                }
                Task::none()
            }
            Message::UtterancesLoaded(id, result) => {
                if !self.is_selected(&id) {
                    return Task::none();
                }
                match result {
                    Ok(u) => {
                        self.blocks = group_blocks(&u);
                        self.utterances = Some(u);
                    }
                    Err(e) => self.notice(e, true),
                }
                Task::none()
            }
            Message::ShowTab(tab) => {
                self.tab = tab;
                Task::none()
            }

            Message::EditTitle(s) => {
                self.title_edit = s;
                Task::none()
            }
            Message::SaveTitle => {
                let Some(d) = &self.detail else { return Task::none() };
                let title = self.title_edit.trim().to_string();
                if title.is_empty() || title == d.title {
                    return Task::none();
                }
                let id = d.id.clone();
                self.notice("Title saved.", false);
                self.change(&id, "rename", json!({ "title": title }))
            }
            Message::ToggleNames => {
                self.show_names = !self.show_names;
                Task::none()
            }
            Message::EditLabel(i, s) => {
                if let Some(e) = self.label_edits.get_mut(i) {
                    e.2 = s;
                }
                Task::none()
            }
            Message::SaveLabels => {
                let Some(d) = &self.detail else { return Task::none() };
                let id = d.id.clone();
                let originals: Vec<(i64, String)> = d
                    .speaker_slots
                    .iter()
                    .filter_map(|s| s.num.map(|n| (n, s.label.clone())))
                    .collect();
                let changes: Vec<(i64, String)> = self
                    .label_edits
                    .iter()
                    .filter(|(n, _, name)| {
                        !name.trim().is_empty()
                            && originals.iter().find(|(o, _)| o == n).map(|(_, l)| l.as_str())
                                != Some(name.trim())
                    })
                    .map(|(n, _, name)| (*n, name.trim().to_string()))
                    .collect();
                self.show_names = false;
                if changes.is_empty() {
                    return Task::none();
                }
                self.notice("Speaker names saved.", false);
                let api = self.api.clone();
                let mid = id.clone();
                Task::perform(
                    api::run(move || {
                        for (num, name) in changes {
                            api.action(&mid, "label", json!({ "speaker": num.to_string(), "name": name }))?;
                        }
                        Ok(Value::Null)
                    }),
                    move |r| Message::Changed(id.clone(), r),
                )
            }
            Message::Summarise => {
                let Some(d) = &self.detail else { return Task::none() };
                let id = d.id.clone();
                self.tab = Tab::Summary;
                self.summary_job = Some(Job { meeting_id: id.clone(), running: true, error: None });
                let api = self.api.clone();
                let mid = id.clone();
                Task::perform(api::run(move || api.action(&mid, "analyze", json!({}))), move |r| {
                    Message::JobStarted(JobKind::Summary, id.clone(), r)
                })
            }
            Message::ToggleIdentify => {
                self.show_identify = !self.show_identify;
                Task::none()
            }
            Message::WhoInput(s) => {
                self.who = s;
                Task::none()
            }
            Message::Identify => {
                let Some(d) = &self.detail else { return Task::none() };
                let id = d.id.clone();
                self.show_identify = false;
                self.tab = Tab::Transcript;
                self.identify_job = Some(Job { meeting_id: id.clone(), running: true, error: None });
                let api = self.api.clone();
                let mid = id.clone();
                let who = self.who.trim().to_string();
                Task::perform(
                    api::run(move || api.action(&mid, "identify", json!({ "who": who }))),
                    move |r| Message::JobStarted(JobKind::Identify, id.clone(), r),
                )
            }
            Message::JobStarted(kind, id, result) => {
                if let Err(e) = result {
                    let job = match kind {
                        JobKind::Summary => &mut self.summary_job,
                        JobKind::Identify => &mut self.identify_job,
                    };
                    *job = Some(Job { meeting_id: id, running: false, error: Some(e) });
                }
                Task::none()
            }
            Message::PollJobs => {
                let mut tasks = Vec::new();
                for (kind, job) in [(JobKind::Summary, &self.summary_job), (JobKind::Identify, &self.identify_job)] {
                    if let Some(j) = job.as_ref().filter(|j| j.running) {
                        tasks.push(self.poll_job(kind, &j.meeting_id));
                    }
                }
                Task::batch(tasks)
            }
            Message::JobPolled(kind, id, result) => {
                let Ok(status) = result else { return Task::none() };
                let slot = match kind {
                    JobKind::Summary => &mut self.summary_job,
                    JobKind::Identify => &mut self.identify_job,
                };
                let tracked = slot.as_ref().is_some_and(|j| same_id(&j.meeting_id, &id));
                if status.running {
                    *slot = Some(Job { meeting_id: id, running: true, error: None });
                    return Task::none();
                }
                if !tracked {
                    // Nothing running; don't show the outcome of an old job
                    // the user didn't start in this window.
                    return Task::none();
                }
                let was_running = slot.as_ref().is_some_and(|j| j.running);
                *slot = Some(Job { meeting_id: id.clone(), running: false, error: status.error.clone() });
                if !was_running {
                    return Task::none();
                }
                let mut tasks = vec![self.fetch_list()];
                if self.is_selected(&id) {
                    tasks.push(self.fetch_detail(&id));
                    if status.error.is_none() {
                        self.notice(
                            match kind {
                                JobKind::Summary => "Summary ready.",
                                JobKind::Identify => "Speakers identified.",
                            },
                            false,
                        );
                    }
                }
                Task::batch(tasks)
            }
            Message::OpenNote => {
                let Some(d) = &self.detail else { return Task::none() };
                if d.note_exists {
                    if let Some(p) = &d.note_path {
                        open_path(p);
                    }
                    return Task::none();
                }
                // No note file yet: have the backend write it, then open it.
                self.busy = Some("Writing the note file…".into());
                let api = self.api.clone();
                let id = d.id.clone();
                Task::perform(api::run(move || api.action(&id, "note", json!({}))), Message::NoteWritten)
            }
            Message::NoteWritten(result) => {
                self.busy = None;
                match result {
                    Ok(v) => {
                        if let Some(p) = v.get("path").and_then(|p| p.as_str()) {
                            open_path(p);
                        }
                        if let Some(d) = &mut self.detail {
                            d.note_exists = true;
                        }
                    }
                    Err(e) => self.notice(e, true),
                }
                Task::none()
            }
            Message::OpenAudio => {
                if let Some(p) = self.detail.as_ref().and_then(|d| d.audio_path.as_ref()) {
                    open_path(p);
                }
                Task::none()
            }
            Message::CopySummary => {
                let Some(d) = &self.detail else { return Task::none() };
                if d.analysis.is_empty() {
                    return Task::none();
                }
                let text = d.analysis.clone();
                self.notice("Summary copied.", false);
                iced::clipboard::write(text)
            }
            Message::AskDelete => {
                self.confirm_delete = true;
                Task::none()
            }
            Message::CancelDelete => {
                self.confirm_delete = false;
                Task::none()
            }
            Message::ConfirmDelete => {
                self.confirm_delete = false;
                let Some(d) = &self.detail else { return Task::none() };
                let api = self.api.clone();
                let id = d.id.clone();
                let mid = id.clone();
                Task::perform(api::run(move || api.action(&mid, "delete", json!({}))), move |r| {
                    Message::Deleted(id.clone(), r)
                })
            }
            Message::Deleted(id, result) => match result {
                Ok(_) => {
                    let title = self.detail.as_ref().map(|d| d.title.clone()).unwrap_or_default();
                    if self.is_selected(&id) {
                        self.selected = None;
                        self.detail = None;
                        self.utterances = None;
                        self.blocks.clear();
                        self.summary.clear();
                    }
                    self.notice(format!("Deleted “{title}”."), false);
                    self.fetch_list()
                }
                Err(e) => {
                    self.notice(e, true);
                    Task::none()
                }
            },

            Message::BlockPick(block, choice) => {
                let Some(b) = self.blocks.get(block) else { return Task::none() };
                if choice == NEW_NAME || choice == RENAME_ALL {
                    self.block_edit = Some(BlockEdit {
                        block,
                        mode: if choice == NEW_NAME { BlockEditMode::NewName } else { BlockEditMode::RenameEverywhere },
                        value: if choice == RENAME_ALL { b.speaker.clone() } else { String::new() },
                    });
                    return Task::none();
                }
                if choice == b.speaker {
                    return Task::none();
                }
                self.set_block_speaker(block, choice)
            }
            Message::BlockEditInput(s) => {
                if let Some(e) = &mut self.block_edit {
                    e.value = s;
                }
                Task::none()
            }
            Message::BlockEditCancel => {
                self.block_edit = None;
                Task::none()
            }
            Message::BlockEditSave => {
                let Some(edit) = self.block_edit.take() else { return Task::none() };
                let name = edit.value.trim().to_string();
                let Some(b) = self.blocks.get(edit.block) else { return Task::none() };
                if name.is_empty() || name == b.speaker {
                    return Task::none();
                }
                match edit.mode {
                    BlockEditMode::NewName => self.set_block_speaker(edit.block, name),
                    BlockEditMode::RenameEverywhere => {
                        let Some(id) = self.selected.clone() else { return Task::none() };
                        let old = b.speaker.clone();
                        self.change(&id, "rename-speaker", json!({ "old": old, "new": name }))
                    }
                }
            }
            Message::Changed(id, result) => {
                if let Err(e) = result {
                    self.notice(e, true);
                }
                let mut tasks = vec![self.fetch_list()];
                if self.is_selected(&id) {
                    tasks.push(self.fetch_detail(&id));
                }
                Task::batch(tasks)
            }
            Message::Link(uri) => {
                open_path(uri.as_str());
                Task::none()
            }
        }
    }

    /// Reassigns every line of one transcript block to `name`.
    fn set_block_speaker(&mut self, block: usize, name: String) -> Task<Message> {
        let (Some(id), Some(u), Some(b)) = (self.selected.clone(), &self.utterances, self.blocks.get(block)) else {
            return Task::none();
        };
        let lines: Vec<i64> = b.lines.iter().map(|&i| u.utterances[i].i).collect();
        let api = self.api.clone();
        let mid = id.clone();
        Task::perform(
            api::run(move || {
                for line in lines {
                    api.action(&mid, "set-speaker", json!({ "line": line, "name": name }))?;
                }
                Ok(Value::Null)
            }),
            move |r| Message::Changed(id.clone(), r),
        )
    }
}

/// Ctrl+R records or stops, Ctrl+F searches, Esc backs out. These work even
/// while a text field has focus.
fn shortcut(event: iced::Event, _status: event::Status, _window: window::Id) -> Option<Message> {
    let iced::Event::Keyboard(keyboard::Event::KeyPressed { key, modifiers, repeat: false, .. }) = event else {
        return None;
    };
    match key.as_ref() {
        keyboard::Key::Named(keyboard::key::Named::Escape) => Some(Message::Escape),
        keyboard::Key::Character("r") if modifiers.control() => Some(Message::RecordPressed),
        keyboard::Key::Character("f") if modifiers.control() => Some(Message::FocusSearch),
        _ => None,
    }
}

fn group_blocks(u: &Utterances) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    for (i, line) in u.utterances.iter().enumerate() {
        match blocks.last_mut() {
            Some(b) if b.speaker == line.speaker => b.lines.push(i),
            _ => blocks.push(Block { speaker: line.speaker.clone(), lines: vec![i], ai: false, tag: String::new() }),
        }
    }
    for b in &mut blocks {
        b.ai = b.lines.iter().any(|&i| u.utterances[i].ai);
        // The most common acoustic tag in the block, shown beside a real name.
        let mut counts: Vec<(&str, usize)> = Vec::new();
        for &i in &b.lines {
            let raw = u.utterances[i].raw.as_str();
            if raw.is_empty() {
                continue;
            }
            match counts.iter_mut().find(|(r, _)| *r == raw) {
                Some(c) => c.1 += 1,
                None => counts.push((raw, 1)),
            }
        }
        let raw = counts.iter().max_by_key(|c| c.1).map(|c| c.0).unwrap_or("");
        if !is_raw_name(&b.speaker) && !raw.is_empty() {
            b.tag = pretty_speaker(raw);
        }
    }
    blocks
}

/// Meetings can be named by a full id or its short prefix.
pub fn same_id(a: &str, b: &str) -> bool {
    !a.is_empty() && !b.is_empty() && (a.starts_with(b) || b.starts_with(a))
}

/// "SPEAKER_01" reads as "Speaker 1".
pub fn pretty_speaker(tag: &str) -> String {
    let lower = tag.to_ascii_lowercase();
    for prefix in ["speaker_", "speaker "] {
        if let Some(rest) = lower.strip_prefix(prefix)
            && let Ok(n) = rest.parse::<u32>() {
                return format!("Speaker {n}");
            }
    }
    tag.to_string()
}

fn is_raw_name(name: &str) -> bool {
    let n = name.trim().to_ascii_lowercase();
    n == "you" || n == "remote" || pretty_speaker(&n) != n
}

/// Opens a file or link with the desktop's default handler, reaping the
/// opener in the background so it never lingers as a zombie.
fn open_path(path: &str) {
    let path = path.to_string();
    std::thread::spawn(move || {
        let _ = std::process::Command::new("xdg-open")
            .arg(path)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pretty_speaker_tags() {
        assert_eq!(pretty_speaker("SPEAKER_01"), "Speaker 1");
        assert_eq!(pretty_speaker("speaker 12"), "Speaker 12");
        assert_eq!(pretty_speaker("Kevin"), "Kevin");
        assert!(is_raw_name("SPEAKER_00"));
        assert!(is_raw_name("You"));
        assert!(!is_raw_name("Kevin"));
    }

    #[test]
    fn groups_consecutive_lines() {
        let u: Utterances = serde_json::from_value(json!({
            "utterances": [
                {"i": 0, "t": "00:01", "text": "a", "raw": "SPEAKER_00", "speaker": "Matt"},
                {"i": 1, "t": "00:02", "text": "b", "raw": "SPEAKER_00", "speaker": "Matt", "ai": true},
                {"i": 2, "t": "00:03", "text": "c", "raw": "SPEAKER_01", "speaker": "SPEAKER_01"},
            ],
            "names": ["Matt", "SPEAKER_01"]
        }))
        .unwrap();
        let b = group_blocks(&u);
        assert_eq!(b.len(), 2);
        assert_eq!(b[0].lines, vec![0, 1]);
        assert!(b[0].ai);
        assert_eq!(b[0].tag, "Speaker 0");
        assert_eq!(b[1].tag, "");
    }
}
