//! A small blocking client for the Verbatim backend's local JSON API
//! (`verbatim gui`, served by verbatim.service on 127.0.0.1:8777).
//!
//! Every call blocks, so the app runs them on Tokio's blocking pool through
//! [`run`]; nothing here touches the UI.

use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::time::Duration;

pub const DEFAULT_URL: &str = "http://127.0.0.1:8777";

#[derive(Debug, Clone)]
pub struct Api {
    base: String,
    agent: ureq::Agent,
}

#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
pub struct Status {
    #[serde(default)]
    pub state: String,
    pub title: Option<String>,
    pub started_at: Option<f64>,
    pub audio: Option<String>,
    pub meeting_id: Option<String>,
    pub message: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MeetingSummary {
    pub id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub started_human: String,
    #[serde(default)]
    pub duration_secs: Option<i64>,
    #[serde(default)]
    pub analyzed: bool,
    #[serde(default)]
    pub match_in: Option<String>,
    #[serde(default)]
    pub snippet: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SpeakerSlot {
    pub num: Option<i64>,
    pub raw: String,
    #[serde(default)]
    pub label: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SpeakerStat {
    pub name: String,
    #[serde(default)]
    pub tag: String,
    #[serde(default)]
    pub words: i64,
    #[serde(default)]
    pub pct: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MeetingDetail {
    pub id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub started_human: String,
    #[serde(default)]
    pub duration_secs: Option<i64>,
    #[serde(default)]
    pub speaker_slots: Vec<SpeakerSlot>,
    #[serde(default)]
    pub analysis: String,
    #[serde(default)]
    pub stats: Vec<SpeakerStat>,
    #[serde(default)]
    pub note_path: Option<String>,
    #[serde(default)]
    pub note_exists: bool,
    #[serde(default)]
    pub audio_path: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Utterance {
    pub i: i64,
    #[serde(default)]
    pub t: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub raw: String,
    #[serde(default)]
    pub speaker: String,
    #[serde(default)]
    pub ai: bool,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Utterances {
    #[serde(default)]
    pub utterances: Vec<Utterance>,
    #[serde(default)]
    pub names: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct JobStatus {
    #[serde(default)]
    pub running: bool,
    pub error: Option<String>,
    #[serde(default)]
    pub done: bool,
}

impl Api {
    /// The backend URL comes from `VERBATIM_URL` when set, so the app can be
    /// pointed at a scratch backend for testing.
    pub fn from_env() -> Self {
        let base = std::env::var("VERBATIM_URL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_URL.to_string());
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(120)))
            .http_status_as_error(false)
            .build();
        Self {
            base: base.trim_end_matches('/').to_string(),
            agent: config.into(),
        }
    }

    pub fn is_default(&self) -> bool {
        self.base == DEFAULT_URL
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    fn get<T: DeserializeOwned>(&self, path: &str) -> Result<T, String> {
        let mut resp = self
            .agent
            .get(&self.url(path))
            .call()
            .map_err(|e| format!("Cannot reach Verbatim at {}: {e}", self.base))?;
        let status = resp.status().as_u16();
        let body = resp.body_mut().read_to_string().map_err(|e| e.to_string())?;
        if status >= 400 {
            return Err(error_text(&body, status));
        }
        serde_json::from_str(&body).map_err(|e| format!("Unexpected reply from Verbatim: {e}"))
    }

    fn post(&self, path: &str, body: Value) -> Result<Value, String> {
        let mut resp = self
            .agent
            .post(&self.url(path))
            .send_json(&body)
            .map_err(|e| format!("Cannot reach Verbatim at {}: {e}", self.base))?;
        let status = resp.status().as_u16();
        let text = resp.body_mut().read_to_string().map_err(|e| e.to_string())?;
        if status >= 400 {
            return Err(error_text(&text, status));
        }
        Ok(serde_json::from_str(&text).unwrap_or(Value::Null))
    }

    pub fn status(&self) -> Result<Status, String> {
        self.get("/api/status")
    }

    pub fn meetings(&self) -> Result<Vec<MeetingSummary>, String> {
        self.get("/api/meetings")
    }

    pub fn search(&self, query: &str) -> Result<Vec<MeetingSummary>, String> {
        self.get(&format!("/api/search?q={}", encode(query)))
    }

    pub fn meeting(&self, id: &str) -> Result<MeetingDetail, String> {
        self.get(&format!("/api/meeting/{}", encode(id)))
    }

    pub fn utterances(&self, id: &str) -> Result<Utterances, String> {
        self.get(&format!("/api/meeting/{}/utterances", encode(id)))
    }

    pub fn analysis_status(&self, id: &str) -> Result<JobStatus, String> {
        self.get(&format!("/api/meeting/{}/analysis-status", encode(id)))
    }

    pub fn identify_status(&self, id: &str) -> Result<JobStatus, String> {
        self.get(&format!("/api/meeting/{}/identify-status", encode(id)))
    }

    pub fn action(&self, id: &str, action: &str, body: Value) -> Result<Value, String> {
        self.post(&format!("/api/meeting/{}/{}", encode(id), action), body)
    }

    pub fn start(&self, title: &str) -> Result<Value, String> {
        self.post("/api/start", json!({ "title": title }))
    }

    pub fn stop(&self) -> Result<Value, String> {
        self.post("/api/stop", json!({}))
    }
}

/// The backend reports failures as `{"error": "..."}`; show that text.
fn error_text(body: &str, status: u16) -> String {
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(str::to_string))
        .unwrap_or_else(|| {
            let body = body.trim();
            if body.is_empty() { format!("Verbatim replied with error {status}") } else { body.to_string() }
        })
}

/// Percent-encodes everything outside the URL-safe set, for paths and queries.
fn encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Runs a blocking API call off the UI thread.
pub async fn run<T, F>(f: F) -> T
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(f).await.expect("API worker panicked")
}
