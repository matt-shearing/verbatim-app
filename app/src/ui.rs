//! The window: top bar, meeting list, and the selected meeting.
//!
//! Every colour comes from the live Omarchy theme and every piece of text
//! uses its font. Plain on purpose: one accent colour, text labels, and one
//! obvious primary action (Record).

use crate::api::{MeetingDetail, MeetingSummary};
use crate::theme::{OmarchyTheme, mix};
use crate::{App, BlockEditMode, Message, NEW_NAME, RENAME_ALL, SEARCH_ID, Tab};
use iced::widget::{
    Space, button, column, container, markdown, pick_list, row, rule, scrollable, text, text_input,
};
use iced::{Alignment, Background, Border, Color, Element, Length, Padding, Theme};

const BODY: f32 = 15.0;
const SMALL: f32 = 13.0;

type El<'a> = Element<'a, Message>;

impl App {
    pub fn view(&self) -> El<'_> {
        let t = &self.theme;
        let body = row![
            container(self.sidebar())
                .width(380)
                .height(Length::Fill)
                .style(panel(t.colors.dark_background)),
            rule::vertical(1).style(hairline(t.line())),
            container(self.detail_view()).width(Length::Fill).height(Length::Fill),
        ];
        column![
            container(self.top_bar())
                .width(Length::Fill)
                .padding([12, 16])
                .style(panel(t.colors.dark_background)),
            rule::horizontal(1).style(hairline(t.line())),
            body,
        ]
        .into()
    }

    // ── helpers that apply the theme font ────────────────────────────────

    fn txt<'a>(&self, s: impl text::IntoFragment<'a>) -> text::Text<'a> {
        text(s).font(self.theme.font).size(BODY)
    }

    fn bold<'a>(&self, s: impl text::IntoFragment<'a>) -> text::Text<'a> {
        text(s).font(self.theme.bold()).size(BODY)
    }

    fn muted<'a>(&self, s: impl text::IntoFragment<'a>) -> text::Text<'a> {
        let c = self.theme.colors.muted;
        text(s).font(self.theme.font).size(SMALL).color(readable(c, self.theme.colors.background))
    }

    fn btn<'a>(&self, label: &'a str, kind: Kind, msg: Option<Message>) -> El<'a> {
        let t = &self.theme;
        button(text(label).font(t.font).size(BODY))
            .padding([7, 14])
            .style(button_style(t, kind))
            .on_press_maybe(msg)
            .into()
    }

    // ── top bar ──────────────────────────────────────────────────────────

    fn top_bar(&self) -> El<'_> {
        let t = &self.theme;
        let c = &t.colors;
        let recording = self.recording();
        let processing = self.processing();
        let reachable = self.status_error.is_none();

        let (label, kind) = if recording { ("Stop", Kind::Stop) } else { ("Record", Kind::Primary) };
        let can_press = reachable && !processing && !self.record_busy;
        let record = button(
            text(label).font(t.bold()).size(17).align_x(Alignment::Center).width(Length::Fill),
        )
        .width(130)
        .padding([10, 18])
        .style(button_style(t, kind))
        .on_press_maybe(can_press.then_some(Message::RecordPressed));

        // While recording, the title is shown beside the timer instead.
        let title: El<'_> = if recording {
            Space::new().into()
        } else {
            let mut input = text_input("Meeting title (optional)", &self.title_input)
                .font(t.font)
                .style(input_style(t))
                .size(BODY)
                .padding(9)
                .width(360);
            if !processing {
                input = input.on_input(Message::TitleInput).on_submit(Message::RecordPressed);
            }
            input.into()
        };

        let status: El<'_> = if let Some(e) = &self.status_error {
            text(format!("Verbatim is not answering. {e}"))
                .font(t.font)
                .size(BODY)
                .color(readable(c.yellow, c.dark_background))
                .into()
        } else {
            match self.status.state.as_str() {
                "recording" => {
                    let secs = self.status.started_at.map(|s| now_secs() - s).unwrap_or(0.0);
                    row![
                        container(Space::new().width(10).height(10)).style(dot(c.red)),
                        text(format!("Recording  {}", clock(secs)))
                            .font(t.bold())
                            .size(17)
                            .color(readable(c.red, c.dark_background)),
                        self.txt(self.status.title.clone().unwrap_or_default()),
                    ]
                    .spacing(12)
                    .align_y(Alignment::Center)
                    .into()
                }
                "processing" => {
                    let msg = progress_text(self.status.message.as_deref().unwrap_or("Processing"));
                    let title = self.status.title.clone().unwrap_or_default();
                    row![self.bold(msg), self.muted(title)].spacing(12).align_y(Alignment::Center).into()
                }
                "error" => text(self.status.message.clone().unwrap_or_else(|| "Something went wrong.".into()))
                    .font(t.font)
                    .size(BODY)
                    .color(readable(c.yellow, c.dark_background))
                    .into(),
                _ => self.muted("Not recording").size(BODY).into(),
            }
        };

        row![record, title, status, Space::new().width(Length::Fill), self.muted("Ctrl+R record · Ctrl+F search")]
            .spacing(16)
            .align_y(Alignment::Center)
            .into()
    }

    // ── meeting list ─────────────────────────────────────────────────────

    fn sidebar(&self) -> El<'_> {
        let t = &self.theme;
        let search = text_input("Search titles, transcripts, summaries", &self.search)
            .id(SEARCH_ID)
            .font(t.font)
            .style(input_style(t))
            .size(BODY)
            .padding(9)
            .on_input(Message::SearchInput);

        let list: El<'_> = if let Some(e) = &self.list_error {
            container(self.txt(e.as_str()).color(t.colors.red)).padding(12).into()
        } else if self.meetings.is_empty() {
            let msg = if self.search.trim().is_empty() { "No meetings yet." } else { "No meetings match." };
            container(self.muted(msg).size(BODY)).padding(12).into()
        } else {
            let items = self.meetings.iter().map(|m| self.list_item(m));
            scrollable(column(items).spacing(2).padding(Padding { right: 12.0, ..Padding::ZERO }))
                .height(Length::Fill)
                .style(scroll_style(t))
                .into()
        };

        let count = if self.search.trim().is_empty() {
            format!("{} meetings", self.meetings.len())
        } else {
            format!("{} found", self.meetings.len())
        };
        column![search, self.muted(count), list].spacing(10).padding(12).into()
    }

    fn list_item<'a>(&'a self, m: &'a MeetingSummary) -> El<'a> {
        let t = &self.theme;
        let selected = self.is_selected(&m.id);
        let title = if m.title.is_empty() { "Untitled meeting" } else { m.title.as_str() };
        let mut meta = row![self.muted(format!(
            "{} · {}",
            nice_date(&m.started_human),
            duration(m.duration_secs)
        ))]
        .spacing(10);
        if m.analyzed {
            meta = meta.push(
                text("✓ Summary").font(t.font).size(SMALL).color(readable(t.colors.accent, t.colors.dark_background)),
            );
        }
        let mut content = column![self.bold(title).wrapping(text::Wrapping::WordOrGlyph), meta].spacing(4);
        if let Some(s) = m.snippet.as_deref().filter(|s| !s.is_empty()) {
            let place = match m.match_in.as_deref() {
                Some("analysis") => "In the summary: ",
                Some("transcript") => "In the transcript: ",
                _ => "",
            };
            content = content.push(self.muted(format!("{place}{s}")));
        }
        let sel = t.selection();
        let hover = mix(t.colors.dark_background, t.colors.foreground, 0.06);
        let fg = t.colors.foreground;
        button(content)
            .width(Length::Fill)
            .padding([9, 12])
            .style(move |_: &Theme, status| button::Style {
                background: Some(Background::Color(if selected {
                    sel
                } else if matches!(status, button::Status::Hovered | button::Status::Pressed) {
                    hover
                } else {
                    Color::TRANSPARENT
                })),
                text_color: fg,
                border: Border::default(),
                ..Default::default()
            })
            .on_press(Message::Select(m.id.clone()))
            .into()
    }

    // ── selected meeting ─────────────────────────────────────────────────

    fn detail_view(&self) -> El<'_> {
        let notice = self.notice_line();
        let Some(d) = &self.detail else {
            let msg = if let Some(e) = &self.detail_error {
                e.clone()
            } else if self.selected.is_some() {
                "Loading…".into()
            } else {
                "Select a meeting on the left, or press Record to start one.".into()
            };
            let mut col = column![self.muted(msg).size(BODY)].spacing(12);
            if let Some(n) = notice {
                col = col.push(n);
            }
            return container(col).center(Length::Fill).into();
        };
        let t = &self.theme;

        let title_changed = self.title_edit.trim() != d.title && !self.title_edit.trim().is_empty();
        let mut title_row = row![
            text_input("Meeting title", &self.title_edit)
                .font(t.bold())
                .size(22)
                .padding(6)
                .on_input(Message::EditTitle)
                .on_submit(Message::SaveTitle)
                .style(quiet_input(t)),
        ]
        .spacing(10)
        .align_y(Alignment::Center);
        if title_changed {
            title_row = title_row.push(self.btn("Save title", Kind::Normal, Some(Message::SaveTitle)));
        }

        let meta = self.muted(format!("{} · {}", nice_date(&d.started_human), duration(d.duration_secs))).size(BODY);

        let summary_running = self.job_running(&self.summary_job, &d.id);
        let identify_running = self.job_running(&self.identify_job, &d.id);
        let has_summary = !d.analysis.is_empty();
        let actions = row![
            self.btn(
                if summary_running {
                    "Summarising…"
                } else if has_summary {
                    "Summarise again"
                } else {
                    "Summarise"
                },
                if has_summary { Kind::Normal } else { Kind::Accent },
                (!summary_running).then_some(Message::Summarise),
            ),
            self.btn(
                if identify_running { "Identifying…" } else { "Identify speakers" },
                Kind::Normal,
                (!identify_running).then_some(Message::ToggleIdentify),
            ),
            self.btn("Name speakers", Kind::Normal, Some(Message::ToggleNames)),
            self.btn("Open note", Kind::Normal, self.busy.is_none().then_some(Message::OpenNote)),
            self.btn("Open audio", Kind::Normal, d.audio_path.is_some().then_some(Message::OpenAudio)),
            self.btn("Copy summary", Kind::Normal, has_summary.then_some(Message::CopySummary)),
            self.btn("Delete", Kind::Danger, Some(Message::AskDelete)),
        ]
        .spacing(8)
        .wrap();

        let mut head = column![title_row, meta, self.talk_time(d), actions].spacing(12);

        if self.confirm_delete {
            head = head.push(self.callout(
                row![
                    self.txt(format!(
                        "Delete “{}” for good? The transcript and summary are removed; the audio file is kept.",
                        d.title
                    ))
                    .width(Length::Fill),
                    self.btn("Delete", Kind::DangerFilled, Some(Message::ConfirmDelete)),
                    self.btn("Cancel", Kind::Normal, Some(Message::CancelDelete)),
                ]
                .spacing(10)
                .align_y(Alignment::Center)
                .into(),
            ));
        }
        if self.show_identify {
            head = head.push(self.callout(
                column![
                    self.txt("Who was there? Optional, but naming people helps the AI split and name voices."),
                    row![
                        text_input("For example: Matt, Kevin, Sarah", &self.who)
                            .font(t.font)
                            .style(input_style(t))
                            .size(BODY)
                            .padding(8)
                            .on_input(Message::WhoInput)
                            .on_submit(Message::Identify),
                        self.btn("Identify", Kind::Accent, Some(Message::Identify)),
                        self.btn("Cancel", Kind::Normal, Some(Message::ToggleIdentify)),
                    ]
                    .spacing(8)
                    .align_y(Alignment::Center),
                ]
                .spacing(8)
                .into(),
            ));
        }
        if self.show_names {
            head = head.push(self.names_editor());
        }
        if let Some(b) = &self.busy {
            head = head.push(self.muted(b.as_str()).size(BODY));
        }
        if let Some(n) = notice {
            head = head.push(n);
        }

        let tabs = row![
            self.tab_button("Summary", Tab::Summary),
            self.tab_button("Transcript", Tab::Transcript),
        ]
        .spacing(4);

        let content: El<'_> = match self.tab {
            Tab::Summary => self.summary_view(d, summary_running),
            Tab::Transcript => self.transcript_view(identify_running),
        };

        column![
            container(head).padding(Padding { top: 18.0, right: 24.0, bottom: 10.0, left: 24.0 }),
            container(tabs).padding([0, 24]),
            rule::horizontal(1).style(hairline(t.line())),
            scrollable(container(content).padding(Padding { top: 16.0, right: 28.0, bottom: 28.0, left: 24.0 }))
                .height(Length::Fill)
                .style(scroll_style(t)),
        ]
        .into()
    }

    fn job_running(&self, job: &Option<crate::Job>, id: &str) -> bool {
        job.as_ref().is_some_and(|j| j.running && crate::same_id(&j.meeting_id, id))
    }

    fn job_error<'a>(&self, job: &'a Option<crate::Job>, id: &str) -> Option<&'a str> {
        job.as_ref().filter(|j| crate::same_id(&j.meeting_id, id)).and_then(|j| j.error.as_deref())
    }

    fn notice_line(&self) -> Option<El<'_>> {
        let n = self.notice.as_ref()?;
        let c = &self.theme.colors;
        let color = if n.error { readable(c.red, c.background) } else { readable(c.green, c.background) };
        Some(text(n.text.as_str()).font(self.theme.font).size(BODY).color(color).into())
    }

    fn callout<'a>(&self, content: El<'a>) -> El<'a> {
        let t = &self.theme;
        let bg = t.colors.dark_background;
        let line = t.line();
        container(content)
            .padding(12)
            .width(Length::Fill)
            .style(move |_: &Theme| container::Style {
                background: Some(bg.into()),
                border: Border { color: line, width: 1.0, radius: 4.0.into() },
                ..Default::default()
            })
            .into()
    }

    fn tab_button<'a>(&self, label: &'a str, tab: Tab) -> El<'a> {
        let t = &self.theme;
        let active = self.tab == tab;
        let accent = t.colors.accent;
        let fg = t.colors.foreground;
        let muted = readable(t.colors.muted, t.colors.background);
        column![
            button(text(label).font(if active { t.bold() } else { t.font }).size(BODY))
                .padding([8, 14])
                .style(move |_: &Theme, status| button::Style {
                    background: None,
                    text_color: if active || matches!(status, button::Status::Hovered) { fg } else { muted },
                    ..Default::default()
                })
                .on_press(Message::ShowTab(tab)),
            container(Space::new().height(3).width(Length::Fill))
                .width(Length::Fill)
                .style(move |_: &Theme| container::Style {
                    background: active.then(|| accent.into()),
                    ..Default::default()
                }),
        ]
        .width(Length::Shrink)
        .into()
    }

    fn talk_time<'a>(&'a self, d: &'a MeetingDetail) -> El<'a> {
        let t = &self.theme;
        let stats: Vec<_> = d.stats.iter().filter(|s| s.pct > 0).collect();
        if stats.is_empty() {
            return Space::new().into();
        }
        let palette = series_colors(t);
        let bar = row(stats.iter().enumerate().map(|(i, s)| {
            let color = palette[i % palette.len()];
            container(Space::new().height(10).width(Length::Fill))
                .width(Length::FillPortion(s.pct.max(1) as u16))
                .style(move |_: &Theme| container::Style { background: Some(color.into()), ..Default::default() })
                .into()
        }))
        .spacing(2);
        let legend = row(stats.iter().enumerate().map(|(i, s)| {
            let color = palette[i % palette.len()];
            let label = if s.tag.is_empty() {
                format!("{} {}%", s.name, s.pct)
            } else {
                format!("{} ({}) {}%", s.name, s.tag, s.pct)
            };
            row![container(Space::new().width(10).height(10)).style(dot(color)), self.txt(label).size(SMALL)]
                .spacing(6)
                .align_y(Alignment::Center)
                .into()
        }))
        .spacing(16)
        .wrap();
        column![
            row![self.muted("Talk time"), container(bar).width(Length::Fill)].spacing(12).align_y(Alignment::Center),
            legend
        ]
        .spacing(8)
        .into()
    }

    fn names_editor(&self) -> El<'_> {
        let t = &self.theme;
        if self.label_edits.is_empty() {
            return self.callout(self.txt("No speakers were detected in this meeting.").into());
        }
        let rows = self.label_edits.iter().enumerate().map(|(i, (_, raw, name))| {
            row![
                self.txt(crate::pretty_speaker(raw)).width(130),
                text_input("Full name", name)
                    .font(t.font)
                    .style(input_style(t))
                    .size(BODY)
                    .padding(7)
                    .width(320)
                    .on_input(move |s| Message::EditLabel(i, s))
                    .on_submit(Message::SaveLabels),
            ]
            .spacing(10)
            .align_y(Alignment::Center)
            .into()
        });
        self.callout(
            column![
                self.txt("Name each speaker. The names are used in the transcript, talk time and note."),
                column(rows).spacing(6),
                row![
                    self.btn("Save names", Kind::Accent, Some(Message::SaveLabels)),
                    self.btn("Cancel", Kind::Normal, Some(Message::ToggleNames)),
                ]
                .spacing(8),
            ]
            .spacing(10)
            .into(),
        )
    }

    fn summary_view<'a>(&'a self, d: &'a MeetingDetail, running: bool) -> El<'a> {
        let t = &self.theme;
        if running {
            return column![
                self.bold("Writing the summary…"),
                self.muted("The local AI takes a minute or two for a long meeting. You can keep working; it shows here when done.")
                    .size(BODY),
            ]
            .spacing(8)
            .into();
        }
        let failed: Option<El<'a>> = self.job_error(&self.summary_job, &d.id).map(|e| {
            self.txt(format!("The summary failed: {e}")).color(readable(t.colors.red, t.colors.background)).into()
        });
        if d.analysis.trim().is_empty() {
            return column![
                column(failed),
                self.txt("No summary yet."),
                self.muted("Summarise runs the local AI on our own hardware; nothing leaves the network.").size(BODY),
                self.btn("Summarise", Kind::Accent, Some(Message::Summarise)),
            ]
            .spacing(10)
            .into();
        }
        let c = &t.colors;
        let style = markdown::Style {
            font: t.font,
            inline_code_highlight: markdown::Highlight {
                background: c.dark_background.into(),
                border: Border { radius: 3.0.into(), ..Default::default() },
            },
            inline_code_padding: Padding { left: 2.0, right: 2.0, ..Padding::ZERO },
            inline_code_color: c.foreground,
            inline_code_font: t.font,
            code_block_font: t.font,
            link_color: readable(c.accent, c.background),
        };
        let mut settings = markdown::Settings::with_text_size(BODY, style);
        settings.h1_size = 24.0.into();
        settings.h2_size = 20.0.into();
        settings.h3_size = 17.0.into();
        settings.h4_size = 16.0.into();
        settings.code_size = 14.0.into();
        settings.spacing = 12.0.into();
        column![column(failed), markdown::view(&self.summary, settings).map(Message::Link)]
            .spacing(16)
            .max_width(1100)
            .into()
    }

    fn transcript_view(&self, running: bool) -> El<'_> {
        let t = &self.theme;
        let Some(u) = &self.utterances else {
            return self.muted("Loading the transcript…").size(BODY).into();
        };
        if u.utterances.is_empty() {
            return self.txt("This meeting has no transcript.").into();
        }
        let mut col = column![].spacing(14).max_width(1100);
        if running {
            col = col.push(self.bold("The AI is working out who said what. This takes a minute or two."));
        } else if let Some(e) = self.selected.as_deref().and_then(|id| self.job_error(&self.identify_job, id)) {
            col = col.push(self.txt(format!("Identifying speakers failed: {e}")).color(readable(t.colors.red, t.colors.background)));
        }
        let any_ai = self.blocks.iter().any(|b| b.ai);
        col = col.push(self.muted(if any_ai {
            "Speakers were identified by the AI. Pick a name beside any block to correct it."
        } else {
            "Pick a name beside any block to change who said it, or use Identify speakers."
        }));

        let mut options: Vec<Choice> = u.names.iter().cloned().map(Choice).collect();
        options.push(Choice(NEW_NAME.into()));
        options.push(Choice(RENAME_ALL.into()));

        let muted = readable(t.colors.muted, t.colors.background);
        for (bi, b) in self.blocks.iter().enumerate() {
            let current = Choice(b.speaker.clone());
            let mut opts = options.clone();
            if !opts.contains(&current) {
                opts.insert(0, current.clone());
            }
            let picker = pick_list(opts, Some(current), move |choice: Choice| Message::BlockPick(bi, choice.0))
                .font(t.bold())
                .text_size(BODY)
                .padding([4, 10])
                .style(picker_style(t));
            let mut header = row![picker].spacing(10).align_y(Alignment::Center);
            if !b.tag.is_empty() {
                header = header.push(self.muted(format!("({})", b.tag)));
            }
            if b.ai {
                header = header.push(self.muted("AI"));
            }
            let mut block = column![header].spacing(4);
            if let Some(edit) = self.block_edit.as_ref().filter(|e| e.block == bi) {
                let prompt = match edit.mode {
                    BlockEditMode::NewName => "Name for this block",
                    BlockEditMode::RenameEverywhere => "Rename this person everywhere to",
                };
                block = block.push(
                    row![
                        text_input(prompt, &edit.value)
                            .font(t.font)
                            .style(input_style(t))
                            .size(BODY)
                            .padding(7)
                            .width(320)
                            .on_input(Message::BlockEditInput)
                            .on_submit(Message::BlockEditSave),
                        self.btn("Save", Kind::Accent, Some(Message::BlockEditSave)),
                        self.btn("Cancel", Kind::Normal, Some(Message::BlockEditCancel)),
                    ]
                    .spacing(8)
                    .align_y(Alignment::Center),
                );
            }
            for &i in &b.lines {
                let line = &u.utterances[i];
                if line.text.trim().is_empty() {
                    continue;
                }
                block = block.push(
                    row![
                        text(format!("{:>5}", line.t)).font(t.font).size(SMALL).color(muted).width(Length::Shrink),
                        self.txt(line.text.replace("**", "")).width(Length::Fill),
                    ]
                    .spacing(12)
                    .align_y(Alignment::Start),
                );
            }
            col = col.push(block);
        }
        col.into()
    }
}

/// A speaker in a block's name picker, shown as "Speaker 1" rather than
/// the raw "SPEAKER_01" tag.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Choice(String);

impl std::fmt::Display for Choice {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&crate::pretty_speaker(&self.0))
    }
}

// ── styles ───────────────────────────────────────────────────────────────

#[derive(Clone, Copy)]
enum Kind {
    /// Record: the one filled accent button.
    Primary,
    /// An accent-filled button inside a panel (Save, Summarise when empty).
    Accent,
    Normal,
    Danger,
    DangerFilled,
    Stop,
}

fn button_style(t: &OmarchyTheme, kind: Kind) -> impl Fn(&Theme, button::Status) -> button::Style + 'static {
    let c = t.colors.clone();
    let on_accent = t.on_accent();
    let line = t.line();
    let on_red = if luminance_hint(c.red) { Color::BLACK } else { Color::WHITE };
    move |_: &Theme, status| {
        let hovered = matches!(status, button::Status::Hovered | button::Status::Pressed);
        let disabled = matches!(status, button::Status::Disabled);
        let (bg, fg, border) = match kind {
            Kind::Primary | Kind::Accent => {
                let bg = if hovered { mix(c.accent, c.foreground, 0.12) } else { c.accent };
                (Some(bg), on_accent, Color::TRANSPARENT)
            }
            Kind::Stop | Kind::DangerFilled => {
                let bg = if hovered { mix(c.red, c.foreground, 0.12) } else { c.red };
                (Some(bg), on_red, Color::TRANSPARENT)
            }
            Kind::Normal => {
                let bg = if hovered { mix(c.background, c.foreground, 0.10) } else { mix(c.background, c.foreground, 0.04) };
                (Some(bg), c.foreground, line)
            }
            Kind::Danger => {
                let bg = if hovered { mix(c.background, c.red, 0.12) } else { mix(c.background, c.foreground, 0.04) };
                (Some(bg), readable(c.red, c.background), line)
            }
        };
        let (bg, fg) = if disabled {
            (bg.map(|b| mix(b, c.background, 0.55)), mix(fg, c.background, 0.5))
        } else {
            (bg, fg)
        };
        button::Style {
            background: bg.map(Background::Color),
            text_color: fg,
            border: Border { color: border, width: 1.0, radius: 4.0.into() },
            ..Default::default()
        }
    }
}

fn picker_style(t: &OmarchyTheme) -> impl Fn(&Theme, pick_list::Status) -> pick_list::Style + 'static {
    let c = t.colors.clone();
    let line = t.line();
    move |_: &Theme, status| {
        let hovered = matches!(status, pick_list::Status::Hovered | pick_list::Status::Opened { .. });
        pick_list::Style {
            text_color: c.foreground,
            placeholder_color: c.muted,
            handle_color: c.foreground,
            background: Background::Color(mix(c.background, c.foreground, if hovered { 0.10 } else { 0.04 })),
            border: Border { color: if hovered { c.accent } else { line }, width: 1.0, radius: 4.0.into() },
        }
    }
}

fn input_style(t: &OmarchyTheme) -> impl Fn(&Theme, text_input::Status) -> text_input::Style + 'static {
    let c = t.colors.clone();
    let line = t.line();
    move |_: &Theme, status| {
        let disabled = matches!(status, text_input::Status::Disabled);
        let focused = matches!(status, text_input::Status::Focused { .. });
        text_input::Style {
            background: Background::Color(if disabled { c.dark_background } else { c.background }),
            border: Border { color: if focused { c.accent } else { line }, width: 1.0, radius: 4.0.into() },
            icon: c.muted,
            placeholder: mix(c.background, c.foreground, 0.45),
            value: if disabled { c.muted } else { c.foreground },
            selection: mix(c.background, c.accent, 0.35),
        }
    }
}

fn quiet_input(t: &OmarchyTheme) -> impl Fn(&Theme, text_input::Status) -> text_input::Style + 'static {
    let c = t.colors.clone();
    let line = t.line();
    move |_: &Theme, status| {
        let active = matches!(status, text_input::Status::Hovered | text_input::Status::Focused { .. });
        text_input::Style {
            background: Background::Color(if active { c.dark_background } else { c.background }),
            border: Border {
                color: if active { line } else { Color::TRANSPARENT },
                width: 1.0,
                radius: 4.0.into(),
            },
            icon: c.muted,
            placeholder: c.muted,
            value: c.foreground,
            selection: mix(c.background, c.accent, 0.35),
        }
    }
}

/// Scrollbars in the theme's own greys rather than iced's generated blue.
fn scroll_style(t: &OmarchyTheme) -> impl Fn(&Theme, scrollable::Status) -> scrollable::Style + 'static {
    let c = t.colors.clone();
    move |theme: &Theme, status| {
        let mut style = scrollable::default(theme, status);
        let active = matches!(status, scrollable::Status::Hovered { .. } | scrollable::Status::Dragged { .. });
        for rail in [&mut style.vertical_rail, &mut style.horizontal_rail] {
            rail.background = None;
            rail.border = Border::default();
            rail.scroller.background = Background::Color(mix(c.background, c.foreground, if active { 0.4 } else { 0.22 }));
            rail.scroller.border = Border { radius: 3.0.into(), ..Default::default() };
        }
        style
    }
}

fn panel(bg: Color) -> impl Fn(&Theme) -> container::Style {
    move |_: &Theme| container::Style { background: Some(bg.into()), ..Default::default() }
}

fn hairline(color: Color) -> impl Fn(&Theme) -> rule::Style {
    move |_: &Theme| rule::Style {
        color,
        radius: 0.0.into(),
        fill_mode: rule::FillMode::Full,
        snap: true,
    }
}

fn dot(color: Color) -> impl Fn(&Theme) -> container::Style {
    move |_: &Theme| container::Style {
        background: Some(color.into()),
        border: Border { radius: 5.0.into(), ..Default::default() },
        ..Default::default()
    }
}

/// Talk-time segments: the accent first, then the theme's other colours.
fn series_colors(t: &OmarchyTheme) -> [Color; 6] {
    let c = &t.colors;
    [c.accent, c.green, c.yellow, c.magenta, c.blue, c.muted]
}

fn luminance_hint(c: Color) -> bool {
    0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b > 0.6
}

/// Nudges a colour towards the text colour until it is legible on `bg`.
/// Light themes often ship a muted grey or yellow too pale to read as text.
fn readable(color: Color, bg: Color) -> Color {
    let contrast = |a: Color, b: Color| {
        let l = |c: Color| {
            let lin = |v: f32| if v <= 0.04045 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) };
            0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)
        };
        let (x, y) = (l(a), l(b));
        (x.max(y) + 0.05) / (x.min(y) + 0.05)
    };
    let target = if 0.2126 * bg.r + 0.7152 * bg.g + 0.0722 * bg.b > 0.5 { Color::BLACK } else { Color::WHITE };
    let mut c = color;
    let mut step = 0.0;
    while contrast(c, bg) < 3.0 && step < 1.0 {
        step += 0.1;
        c = mix(color, target, step);
    }
    c
}

// ── formatting ───────────────────────────────────────────────────────────

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// m:ss, or h:mm:ss past an hour.
pub fn clock(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    let (h, m, s) = (s / 3600, s % 3600 / 60, s % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m}:{s:02}") }
}

pub fn duration(secs: Option<i64>) -> String {
    let s = secs.unwrap_or(0).max(0);
    if s < 60 {
        format!("{s} s")
    } else if s < 3600 {
        format!("{} min", (s + 30) / 60)
    } else {
        let m = (s + 30) / 60;
        format!("{} h {} min", m / 60, m % 60)
    }
}

/// "2026-09-30 11:37" reads as "30 Sep 2026, 11:37".
pub fn nice_date(s: &str) -> String {
    const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let (date, time) = s.split_once(' ').unwrap_or((s, ""));
    let parts: Vec<&str> = date.split('-').collect();
    if let [y, m, d] = parts.as_slice() {
        if let (Ok(m), Ok(d)) = (m.parse::<usize>(), d.parse::<u32>()) {
            if (1..=12).contains(&m) {
                let base = format!("{d} {} {y}", MONTHS[m - 1]);
                return if time.is_empty() { base } else { format!("{base}, {time}") };
            }
        }
    }
    s.to_string()
}

/// The backend says "Transcribing (40%)"; show "Transcribing 40%".
pub fn progress_text(msg: &str) -> String {
    if let Some(open) = msg.rfind(" (") {
        let inner = &msg[open + 2..];
        if let Some(pct) = inner.strip_suffix(')') {
            if pct.ends_with('%') {
                return format!("{} {}", &msg[..open], pct);
            }
        }
    }
    msg.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats() {
        assert_eq!(clock(75.0), "1:15");
        assert_eq!(clock(3723.0), "1:02:03");
        assert_eq!(duration(Some(4810)), "1 h 20 min");
        assert_eq!(duration(Some(1384)), "23 min");
        assert_eq!(nice_date("2026-09-30 11:37"), "30 Sep 2026, 11:37");
        assert_eq!(progress_text("Transcribing (40%)"), "Transcribing 40%");
        assert_eq!(progress_text("Writing the summary"), "Writing the summary");
    }
}
