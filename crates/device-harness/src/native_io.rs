// SPDX-License-Identifier: AGPL-3.0-only
//! Concrete session/file/loopback composition over injected native effects.
//! The native file/wire/OS implementations live in `native_files`, `native_wire` and
//! `native_os`; there is deliberately no launcher or installed-state implementation.
use crate::{
    Fault, FaultKind, Result,
    evidence_inputs::Inputs,
    native_session::{self, Binding, LaunchForm, Observation, Reply, Request, Session},
};
use budgets::{Bytes, Span};
use std::net::SocketAddrV4;
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum Slot {
    Control,
    Launch,
    Session,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
}
/// Contains private data: intentionally no Debug/Serialize.
pub struct PrivateFile {
    pub identity: FileIdentity,
    pub uid: u32,
    pub mode: u32,
    pub links: u64,
    pub regular: bool,
    pub symlink: bool,
    pub modified_ms: u64,
    pub bytes: Vec<u8>,
}
pub trait Files {
    /// Independent package/process/control observations, never status-attested.
    fn observe(&mut self, bound: Span) -> Result<Observation>;
    /// Bound the read itself, not only the returned allocation. No symlink parents.
    fn read(&mut self, slot: Slot, max_bytes: u64, bound: Span) -> Result<Option<PrivateFile>>;
    /// In an exclusively owned private parent, compare old identity then atomically
    /// replace with a new single-link owned 0600 file; fsync file and parent.
    fn replace_session(
        &mut self,
        old: Option<FileIdentity>,
        bytes: &[u8],
        bound: Span,
    ) -> Result<()>;
}
pub trait Wire {
    /// Connect only to this address. No proxy, DNS, redirects or retry.
    fn send(&mut self, address: SocketAddrV4, request: Request, bound: Span) -> Result<Reply>;
}
/// Mandatory typed caller budgets; no guessed operational defaults. The native
/// provider must source each cap from its admitted L1 resource policy.
#[derive(Clone, Copy)]
pub struct FileLimits {
    pub control: Bytes,
    pub launch: Bytes,
    pub session: Bytes,
}
pub struct NativeIo<F, W, C> {
    pub files: F,
    pub wire: W,
    clock: C,
    limits: FileLimits,
    uid: u32,
    binding: Binding,
    observed: Option<Observation>,
}
fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Control {
    port: u16,
    credential: String,
}
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredSession {
    binding: Binding,
    port: u16,
    cookie: String,
}
fn token(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}
impl<F: Files, W: Wire, C: crate::effects::Clock> NativeIo<F, W, C> {
    pub fn new(
        files: F,
        wire: W,
        uid: u32,
        binding: Binding,
        inputs: &Inputs,
        clock: C,
        limits: FileLimits,
    ) -> Result<Self> {
        if binding.native_inputs_sha256 != inputs.native_identity().sha256 {
            return Err(fault("native_evidence_binding_mismatch"));
        }
        if [limits.control, limits.launch, limits.session].contains(&Bytes::ZERO) {
            return Err(fault("native_file_budget_invalid"));
        }
        Ok(Self {
            limits,
            files,
            wire,
            clock,
            uid,
            binding,
            observed: None,
        })
    }
    fn end(&self, bound: Span) -> Result<u64> {
        self.clock
            .now()
            .checked_add(bound.millis())
            .filter(|e| *e > self.clock.now())
            .ok_or(fault("native_io_timeout"))
    }
    fn remaining(&self, end: u64) -> Result<Span> {
        end.checked_sub(self.clock.now())
            .filter(|n| *n > 0)
            .map(Span::from_millis)
            .ok_or(fault("native_io_timeout"))
    }
    fn read(&mut self, slot: Slot, end: u64) -> Result<Option<PrivateFile>> {
        let max = match slot {
            Slot::Control => self.limits.control,
            Slot::Launch => self.limits.launch,
            Slot::Session => self.limits.session,
        }
        .get();
        let file = self.files.read(slot, max, self.remaining(end)?)?;
        self.remaining(end)?;
        if let Some(f) = &file
            && (!f.regular
                || f.symlink
                || f.uid != self.uid
                || f.mode != 0o600
                || f.links != 1
                || f.bytes.len() as u64 > max)
        {
            return Err(fault("native_private_file_invalid"));
        }
        Ok(file)
    }
    fn control(&mut self, end: u64) -> Result<(Control, u64)> {
        let f = self
            .read(Slot::Control, end)?
            .ok_or(fault("native_control_missing"))?;
        let c: Control =
            serde_json::from_slice(&f.bytes).map_err(|_| fault("native_control_invalid"))?;
        if c.port == 0 || !token(&c.credential) || f.modified_ms < self.binding.launch_epoch_ms {
            return Err(fault("native_control_invalid"));
        }
        Ok((c, f.modified_ms))
    }
}
impl<F: Files, W: Wire, C: crate::effects::Clock> native_session::Io for NativeIo<F, W, C> {
    fn observe(&mut self, bound: Span) -> Result<Observation> {
        self.observed = None;
        let end = self.end(bound)?;
        let o = self.files.observe(self.remaining(end)?)?;
        let (control, modified) = self.control(end)?;
        if o.binding != self.binding || o.port != control.port || o.control_modified_ms != modified
        {
            return Err(fault("native_observation_mismatch"));
        }
        self.observed = Some(o.clone());
        Ok(o)
    }
    fn load_session(&mut self, bound: Span) -> Result<Option<Session>> {
        let end = self.end(bound)?;
        self.read(Slot::Session, end)?
            .map(|file| {
                let record: StoredSession = serde_json::from_slice(&file.bytes)
                    .map_err(|_| fault("native_session_record_invalid"))?;
                if record.binding != self.binding
                    || !record.cookie.strip_prefix("a13session=").is_some_and(token)
                {
                    return Err(fault("native_session_record_invalid"));
                }
                Ok(Session {
                    binding: record.binding,
                    port: record.port,
                    cookie: record.cookie,
                })
            })
            .transpose()
    }
    fn native_credential(&mut self, bound: Span) -> Result<String> {
        let (control, modified) = self.control(self.end(bound)?)?;
        if !self
            .observed
            .as_ref()
            .is_some_and(|o| o.port == control.port && o.control_modified_ms == modified)
        {
            return Err(fault("native_observation_mismatch"));
        }
        Ok(control.credential)
    }
    fn launch_form(&mut self, bound: Span) -> Result<LaunchForm> {
        let f = self
            .read(Slot::Launch, self.end(bound)?)?
            .ok_or(fault("native_launch_file_missing"))?;
        parse_launch(&f.bytes, f.modified_ms)
    }
    fn exchange(&mut self, request: Request, bound: Span) -> Result<Reply> {
        let end = self.end(bound)?;
        if request.follow_redirects
            || self
                .observed
                .as_ref()
                .is_none_or(|o| o.port != request.port)
            || request
                .headers
                .iter()
                .any(|(k, v)| k.contains(['\r', '\n', '\0']) || v.contains(['\r', '\n', '\0']))
            || !matches!(
                (request.method, request.path),
                (
                    "POST",
                    "/native-session"
                        | "/session"
                        | "/api/start"
                        | "/api/capability/report"
                        | "/api/join/window"
                        | "/api/join/window/poll"
                ) | ("GET", "/api/status")
            )
        {
            return Err(fault("native_loopback_request_refused"));
        }
        let headers: std::collections::BTreeMap<_, _> = request
            .headers
            .iter()
            .map(|(k, v)| (k.to_ascii_lowercase(), v.as_str()))
            .collect();
        let expected: &[&str] = match request.path {
            "/native-session" => &["content-type", "x-native-authorization"],
            "/session" => &["content-type", "origin"],
            "/api/status" => &["cookie"],
            _ => &["content-type", "cookie", "origin", "x-mycelium-action"],
        };
        let cookie = || {
            headers
                .get("cookie")
                .is_some_and(|s| s.strip_prefix("a13session=").is_some_and(token))
        };
        let valid = match request.path {
            "/native-session" => {
                headers.get("content-type") == Some(&"application/json")
                    && headers
                        .get("x-native-authorization")
                        .is_some_and(|s| token(s))
                    && request.body == b"{}"
            }
            "/session" => {
                headers.get("content-type") == Some(&"application/x-www-form-urlencoded")
                    && headers.get("origin") == Some(&"null")
                    && std::str::from_utf8(&request.body)
                        .ok()
                        .and_then(|s| s.strip_prefix("launch="))
                        .is_some_and(token)
            }
            "/api/status" => cookie() && request.body.is_empty(),
            _ => {
                cookie()
                    && headers.get("content-type") == Some(&"application/json")
                    && headers.get("x-mycelium-action") == Some(&"1")
                    && headers
                        .get("origin")
                        .is_some_and(|s| *s == format!("http://127.0.0.1:{}", request.port))
                    && serde_json::from_slice::<serde_json::Value>(&request.body)
                        .is_ok_and(|v| v.is_object())
            }
        };
        if headers.len() != request.headers.len()
            || headers.len() != expected.len()
            || !expected.iter().all(|k| headers.contains_key(*k))
            || !valid
        {
            return Err(fault("native_loopback_request_refused"));
        }
        let address = SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, request.port);
        let reply = self.wire.send(address, request, self.remaining(end)?)?;
        self.remaining(end)?;
        Ok(reply)
    }
    fn save_session(&mut self, session: &Session, bound: Span) -> Result<()> {
        let end = self.end(bound)?;
        if session.binding != self.binding
            || !session
                .cookie
                .strip_prefix("a13session=")
                .is_some_and(token)
            || self
                .observed
                .as_ref()
                .is_none_or(|o| o.port != session.port)
        {
            return Err(fault("native_session_record_invalid"));
        }
        let old = self.read(Slot::Session, end)?.map(|f| f.identity);
        let bytes = serde_json::to_vec(&StoredSession {
            binding: session.binding.clone(),
            port: session.port,
            cookie: session.cookie.clone(),
        })
        .map_err(|_| fault("native_session_encoding_failed"))?;
        if bytes.len() as u64 > self.limits.session.get() {
            return Err(fault("native_session_record_invalid"));
        }
        self.files
            .replace_session(old, &bytes, self.remaining(end)?)?;
        let readback = self
            .read(Slot::Session, end)?
            .ok_or(fault("native_session_readback_missing"))?;
        if readback.bytes != bytes {
            return Err(fault("native_session_readback_mismatch"));
        }
        Ok(())
    }
}
/// Accept only the pinned native launch-file form grammar (app.py:874-880),
/// not arbitrary browser HTML. Never execute the document or follow its URLs.
fn parse_launch(bytes: &[u8], modified_ms: u64) -> Result<LaunchForm> {
    let invalid = || fault("native_launch_form_invalid");
    let html = std::str::from_utf8(bytes).map_err(|_| invalid())?;
    let lower = html.to_ascii_lowercase();
    if html.contains('\0')
        || lower.matches("<form").count() != 1
        || lower.matches("</form>").count() != 1
        || lower.matches("<input").count() != 1
    {
        return Err(invalid());
    }
    let (_, form) = html
        .split_once("<form method=\"post\" action=\"http://127.0.0.1:")
        .ok_or_else(invalid)?;
    let (port, form) = form.split_once("/session\">").ok_or_else(invalid)?;
    let parsed: u16 = port.parse().map_err(|_| invalid())?;
    if parsed == 0 || parsed.to_string() != port {
        return Err(invalid());
    }
    let form = form
        .trim_start()
        .strip_prefix("<input type=\"hidden\" name=\"launch\" value=\"")
        .ok_or_else(invalid)?;
    let (launch, tail) = form.split_once("\">").ok_or_else(invalid)?;
    if !token(launch) || !tail.contains("</form>") {
        return Err(invalid());
    }
    Ok(LaunchForm {
        port: parsed,
        path: "/session".into(),
        launch: launch.into(),
        modified_ms,
    })
}
