//! End-to-end checks of the app's update loop against a live backend.
//!
//! These mutate meetings, so they only run when `VERBATIM_TEST_URL` names a
//! scratch backend (never the real one):
//!
//! ```sh
//! VERBATIM_TEST_URL=http://127.0.0.1:8790 cargo test -- --ignored --nocapture
//! ```
//!
//! The scratch backend must hold at least two meetings, one of them already
//! summarised, and its AI endpoint should be unreachable so the summary and
//! identify jobs fail fast. The test works on whichever meetings it finds.

use crate::*;
use iced::futures::StreamExt;
use iced_runtime::{Action, task::into_stream};

fn backend() -> Option<String> {
    let url = std::env::var("VERBATIM_TEST_URL").ok()?;
    assert!(!url.contains(":8777"), "refusing to run mutating tests against the real backend");
    Some(url)
}

/// Runs a task and every task its messages lead to, like the iced runtime.
fn drive(rt: &tokio::runtime::Runtime, app: &mut App, task: Task<Message>) {
    let mut queue = vec![task];
    while let Some(task) = queue.pop() {
        let Some(stream) = into_stream(task) else { continue };
        let actions: Vec<_> = rt.block_on(stream.collect());
        for action in actions {
            if let Action::Output(msg) = action {
                queue.push(app.update(msg));
            }
        }
    }
}

fn send(rt: &tokio::runtime::Runtime, app: &mut App, msg: Message) {
    let task = app.update(msg);
    drive(rt, app, task);
}

#[test]
#[ignore]
fn mutating_flows_against_scratch_backend() {
    let Some(url) = backend() else {
        eprintln!("VERBATIM_TEST_URL not set; skipping");
        return;
    };
    // SAFETY: set before any other thread reads the environment.
    unsafe { std::env::set_var("VERBATIM_URL", &url) };
    let rt = tokio::runtime::Builder::new_current_thread().enable_time().build().unwrap();
    let _guard = rt.enter();
    let api = Api::from_env();
    let (mut app, boot) = App::new(OmarchyTheme::load(), Options::default());
    drive(&rt, &mut app, boot);
    assert_eq!(app.status.state, "idle");
    assert!(app.meetings.len() >= 2, "scratch list: {:?}", app.meetings.len());
    let first = app.meetings.iter().find(|m| m.analyzed).expect("a summarised meeting").clone();
    let other = app.meetings.iter().find(|m| m.id != first.id).unwrap().clone();
    let id = first.id[..8].to_string();
    let renamed = format!("{} (scratch)", first.title);

    // Open a meeting by its short id.
    send(&rt, &mut app, Message::Select(id.clone()));
    let d = app.detail.clone().expect("detail loaded");
    assert_eq!(d.title, first.title);
    assert!(app.selected.as_deref() == Some(d.id.as_str()), "short id resolved to full");
    assert!(!app.summary.is_empty(), "summary parsed");
    assert!(app.blocks.len() > 3, "transcript grouped");

    // Rename.
    send(&rt, &mut app, Message::EditTitle(renamed.clone()));
    send(&rt, &mut app, Message::SaveTitle);
    assert_eq!(api.meeting(&id).unwrap().title, renamed);
    assert_eq!(app.detail.as_ref().unwrap().title, renamed);
    assert!(app.meetings.iter().any(|m| m.title == renamed));

    // Name a speaker.
    send(&rt, &mut app, Message::ToggleNames);
    let slot = app.label_edits.iter().position(|(n, _, _)| *n == 1).expect("speaker 1");
    send(&rt, &mut app, Message::EditLabel(slot, "Test Person".into()));
    send(&rt, &mut app, Message::SaveLabels);
    let labels = api.meeting(&id).unwrap().speaker_slots;
    assert!(labels.iter().any(|s| s.num == Some(1) && s.label == "Test Person"), "{labels:?}");

    // Give one transcript block a new name.
    send(&rt, &mut app, Message::Select(id.clone()));
    let block = 1;
    let first_line = app.blocks[block].lines[0];
    let line_i = app.utterances.as_ref().unwrap().utterances[first_line].i;
    send(&rt, &mut app, Message::BlockPick(block, NEW_NAME.into()));
    assert!(app.block_edit.is_some());
    send(&rt, &mut app, Message::BlockEditInput("Someone Else".into()));
    send(&rt, &mut app, Message::BlockEditSave);
    let u = api.utterances(&id).unwrap();
    assert_eq!(u.utterances.iter().find(|x| x.i == line_i).unwrap().speaker, "Someone Else");

    // Rename that person everywhere.
    let block = app.blocks.iter().position(|b| b.speaker == "Someone Else").expect("renamed block");
    send(&rt, &mut app, Message::BlockPick(block, RENAME_ALL.into()));
    send(&rt, &mut app, Message::BlockEditInput("Another Name".into()));
    send(&rt, &mut app, Message::BlockEditSave);
    let u = api.utterances(&id).unwrap();
    assert!(u.utterances.iter().any(|x| x.speaker == "Another Name"));
    assert!(!u.utterances.iter().any(|x| x.speaker == "Someone Else"));

    // Summarise: the job starts, is polled, and reports the AI failure.
    send(&rt, &mut app, Message::Summarise);
    assert!(app.summary_job.as_ref().unwrap().running);
    for _ in 0..60 {
        if !app.summary_job.as_ref().unwrap().running {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
        send(&rt, &mut app, Message::PollJobs);
    }
    let job = app.summary_job.clone().unwrap();
    assert!(!job.running);
    eprintln!("summary job finished: error = {:?}", job.error);

    // Identify speakers with a roster.
    send(&rt, &mut app, Message::ToggleIdentify);
    send(&rt, &mut app, Message::WhoInput("Alex, Sam".into()));
    send(&rt, &mut app, Message::Identify);
    for _ in 0..60 {
        if !app.identify_job.as_ref().unwrap().running {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
        send(&rt, &mut app, Message::PollJobs);
    }
    let job = app.identify_job.clone().unwrap();
    assert!(!job.running);
    eprintln!("identify job finished: error = {:?}", job.error);

    // The note endpoint writes a file into the scratch notes directory.
    let note = api.action(&id, "note", serde_json::json!({})).unwrap();
    eprintln!("note written: {note}");
    assert!(std::path::Path::new(note["path"].as_str().unwrap()).exists());

    // Search, then Escape clears it.
    let word = other.title.split_whitespace().next().unwrap().to_string();
    send(&rt, &mut app, Message::SearchInput(word));
    assert!(app.meetings.iter().any(|m| m.id == other.id));
    send(&rt, &mut app, Message::Escape);
    assert!(app.search.is_empty());
    assert!(app.meetings.len() >= 2);

    // Delete, with confirmation.
    send(&rt, &mut app, Message::Select(other.id.clone()));
    send(&rt, &mut app, Message::AskDelete);
    assert!(app.confirm_delete);
    send(&rt, &mut app, Message::Escape);
    assert!(!app.confirm_delete, "Escape cancels the confirmation");
    send(&rt, &mut app, Message::AskDelete);
    send(&rt, &mut app, Message::ConfirmDelete);
    assert!(app.selected.is_none());
    assert!(!app.meetings.iter().any(|m| m.id == other.id));
    assert!(api.meeting(&other.id).is_err());
}
