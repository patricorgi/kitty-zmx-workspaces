//! Safety-critical workspace primitives being migrated from Python.
//!
//! This library is not yet connected to the running workspace manager.

use std::collections::{BTreeMap, HashSet};
use std::io::{self, Write};
use std::os::unix::fs::DirBuilderExt;
use std::path::Path;

use serde_json::Value;

const FLAG: &str = "kitty-unserialize-data=";
const FOCUS_PREFIX: &str = "# kzw-focus ";
const DORMANT: &str = "# kzw-dormant-workspace\n";
const EMPTY: &str = "# kzw-empty-workspace\n";
const ORIGIN: &str = "KZW_ORIGIN";
const EXECUTABLE: &str = "KZW_PANE_EXECUTABLE";

/// Replace a snapshot only after its complete contents have been flushed.
/// A private temporary file is created in the target directory so rename is
/// atomic on the same filesystem. A failed write leaves the old file intact.
pub fn atomic_write(path: &Path, contents: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(parent)?;
    let mut temporary = tempfile::Builder::new()
        .prefix(".snapshot-")
        .tempfile_in(parent)?;
    temporary.write_all(contents)?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pane {
    pub id: String,
    pub host: String,
}

pub fn valid_pane_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("kzw-")
        && value[4..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn valid_workspace_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 80
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(b))
}

pub fn valid_host(value: &str) -> bool {
    value.is_empty()
        || (!value.starts_with('-')
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.@:-".contains(&b)))
}

/// Extract one stable session identity per Kitty window, rejecting incomplete
/// and duplicate observations rather than saving a partial layout.
pub fn pane_map(tree: &Value) -> Result<BTreeMap<i64, Pane>, String> {
    let windows = tree.as_array().ok_or("invalid Kitty window list")?;
    let mut result = BTreeMap::new();
    let mut ids = HashSet::new();
    for os_window in windows {
        for tab in array_field(os_window, "tabs")? {
            for window in array_field(tab, "windows")? {
                let vars = window
                    .get("user_vars")
                    .and_then(Value::as_object)
                    .ok_or("unmanaged Kitty pane")?;
                let pane = vars.get("kzw_pane").and_then(Value::as_str).unwrap_or("");
                let host = match vars.get("kzw_host") {
                    None => "",
                    Some(value) => value.as_str().ok_or("invalid pane host")?,
                };
                let id = window
                    .get("id")
                    .and_then(Value::as_i64)
                    .ok_or("invalid Kitty window ID")?;
                if !valid_pane_id(pane) || !valid_host(host) || !ids.insert(pane.to_owned()) {
                    return Err("invalid or duplicate managed pane identity".into());
                }
                if result
                    .insert(
                        id,
                        Pane {
                            id: pane.into(),
                            host: host.into(),
                        },
                    )
                    .is_some()
                {
                    return Err("duplicate Kitty window ID".into());
                }
            }
        }
    }
    if result.is_empty() {
        return Err("workspace has no managed panes".into());
    }
    Ok(result)
}

fn array_field<'a>(value: &'a Value, name: &str) -> Result<&'a [Value], String> {
    match value.get(name) {
        None => Ok(&[]),
        Some(value) => value
            .as_array()
            .map(Vec::as_slice)
            .ok_or_else(|| format!("invalid {name}")),
    }
}

/// Identify focus by stable pane identity, never by a transient Kitty ID.
pub fn focused_pane(tree: &Value) -> Result<String, String> {
    let windows = tree.as_array().ok_or("invalid Kitty window list")?;
    let mut selected: Vec<_> = windows
        .iter()
        .filter(|w| w.get("last_focused") == Some(&Value::Bool(true)))
        .collect();
    if selected.is_empty() {
        selected = windows
            .iter()
            .filter(|w| w.get("is_focused") == Some(&Value::Bool(true)))
            .collect();
    }
    if selected.len() != 1 {
        return Err("cannot identify focused OS window".into());
    }
    let tabs: Vec<_> = array_field(selected[0], "tabs")?
        .iter()
        .filter(|t| t.get("is_active") == Some(&Value::Bool(true)))
        .collect();
    if tabs.len() != 1 {
        return Err("cannot identify active tab".into());
    }
    let panes: Vec<_> = array_field(tabs[0], "windows")?
        .iter()
        .filter(|w| w.get("is_active") == Some(&Value::Bool(true)))
        .collect();
    if panes.len() != 1 {
        return Err("cannot identify active pane".into());
    }
    let pane = panes[0]
        .get("user_vars")
        .and_then(|v| v.get("kzw_pane"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !valid_pane_id(pane) {
        return Err("focused pane is not managed".into());
    }
    Ok(pane.into())
}

pub fn saved_focus(snapshot: &str) -> Result<Option<String>, String> {
    for line in snapshot.lines() {
        if let Some(raw) = line.strip_prefix(FOCUS_PREFIX) {
            let value: Value =
                serde_json::from_str(raw).map_err(|_| "invalid saved focus metadata")?;
            let pane = value.get("pane").and_then(Value::as_str).unwrap_or("");
            if !valid_pane_id(pane) {
                return Err("invalid saved focus metadata".into());
            }
            return Ok(Some(pane.into()));
        }
    }
    Ok(None)
}

/// Convert between a local snapshot and the portable remote-origin format.
pub fn translate(
    snapshot: &str,
    executable: &str,
    host: &str,
    portable: bool,
) -> Result<(String, Vec<String>), String> {
    if snapshot == DORMANT || snapshot == EMPTY {
        return Ok((snapshot.into(), vec![]));
    }
    let mut lines = Vec::new();
    let mut found = Vec::new();
    let mut seen = HashSet::new();
    for line in snapshot.lines() {
        if line.starts_with("cd ") {
            continue;
        }
        if !line.starts_with("launch ") {
            lines.push(line.to_owned());
            continue;
        }
        let args = shlex::split(line).ok_or("invalid launch quoting")?;
        if args.len() < 7 || !args[1].starts_with(FLAG) {
            return Err("unsupported remote launch".into());
        }
        let command = &args[args.len() - 6..];
        if command[1] != "--pane"
            || command[3] != "--host"
            || command[5] != "--existing"
            || !valid_pane_id(&command[2])
        {
            return Err("remote workspace contains an unmanaged launch".into());
        }
        if (portable && command[4] != host)
            || (!portable && (command[0] != EXECUTABLE || command[4] != ORIGIN))
        {
            return Err("remote workspace origin does not match".into());
        }
        if !seen.insert(command[2].clone()) {
            return Err("duplicate remote pane".into());
        }
        found.push(command[2].clone());
        let mut rewritten: Vec<String> = args[..args.len() - 6]
            .iter()
            .filter(|arg| !arg.starts_with("--cwd=") && !arg.starts_with("--var=kzw_host="))
            .cloned()
            .collect();
        rewritten.push(format!(
            "--var=kzw_host={}",
            if portable { ORIGIN } else { host }
        ));
        rewritten.extend(
            [
                if portable { EXECUTABLE } else { executable },
                "--pane",
                &command[2],
                "--host",
                if portable { ORIGIN } else { host },
                "--existing",
            ]
            .into_iter()
            .map(str::to_owned),
        );
        lines.push(
            rewritten
                .iter()
                .map(|a| shell_quote(a))
                .collect::<Vec<_>>()
                .join(" "),
        );
    }
    if found.is_empty() {
        return Err("remote workspace has no managed panes".into());
    }
    found.sort();
    Ok((lines.join("\n") + "\n", found))
}

/// Preserve Kitty layout lines but replace all launch commands with existing
/// session attachments. Never replay a saved foreground command.
pub fn rewrite_export(
    text: &str,
    panes: &BTreeMap<i64, Pane>,
    executable: &str,
) -> Result<String, String> {
    let mut lines = Vec::new();
    let mut seen = HashSet::new();
    for line in text.lines() {
        if !line.starts_with("launch ") {
            lines.push(line.to_owned());
            continue;
        }
        let args = shlex::split(line).ok_or("invalid Kitty launch quoting")?;
        let raw = args
            .get(1)
            .and_then(|s| s.strip_prefix(FLAG))
            .ok_or("missing serialized window data")?;
        let mut data: Value =
            serde_json::from_str(raw).map_err(|_| "invalid serialized window data")?;
        let object = data
            .as_object_mut()
            .ok_or("invalid serialized window data")?;
        let id = object
            .get("id")
            .and_then(Value::as_i64)
            .ok_or("invalid serialized window ID")?;
        let pane = panes.get(&id).ok_or("layout changed during snapshot")?;
        if !seen.insert(id) {
            return Err("duplicate window in layout export".into());
        }
        object.remove("cmd_at_shell_startup");
        let mut launch = vec!["launch".to_owned(), format!("{FLAG}{data}")];
        for arg in args.iter().skip(2).take_while(|arg| arg.starts_with("--")) {
            if !arg.starts_with("--var=kzw_attach_ended=") && !arg.starts_with("--var=kzw_staging=")
            {
                launch.push(arg.clone());
            }
        }
        launch.extend(
            [
                executable,
                "--pane",
                &pane.id,
                "--host",
                &pane.host,
                "--existing",
            ]
            .into_iter()
            .map(str::to_owned),
        );
        lines.push(
            launch
                .iter()
                .map(|arg| shell_quote(arg))
                .collect::<Vec<_>>()
                .join(" "),
        );
    }
    if seen.len() != panes.len() {
        return Err("layout export did not include every managed pane".into());
    }
    Ok(lines.join("\n") + "\n")
}

// Match Python shlex.quote: keep only shell-safe ASCII characters unquoted.
fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        return value.into();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const A: &str = "kzw-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const B: &str = "kzw-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn tree() -> Value {
        json!([{ "tabs": [{ "windows": [
            {"id": 7, "user_vars": {"kzw_pane": A, "kzw_host": ""}},
            {"id": 8, "user_vars": {"kzw_pane": B, "kzw_host": "dev.example"}}
        ] }]}])
    }

    #[test]
    fn validates_workspace_pane_and_host_names() {
        assert!(valid_workspace_name("Test"));
        assert!(!valid_workspace_name("../Test"));
        assert!(!valid_workspace_name(&"a".repeat(81)));
        assert!(valid_pane_id(A));
        assert!(!valid_pane_id(&A.to_uppercase()));
        assert!(valid_host("user@host.example"));
        assert!(!valid_host("-oProxyCommand=bad"));
        assert!(!valid_host("host;bad"));
    }

    #[test]
    fn atomic_snapshot_replaces_contents_with_private_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("Test").join("snapshot");
        atomic_write(&target, b"first").unwrap();
        atomic_write(&target, b"second").unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"second");
        assert_eq!(
            std::fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::read_dir(target.parent().unwrap()).unwrap().count(),
            1
        );
    }

    #[test]
    fn rejects_missing_unmanaged_or_duplicate_windows() {
        assert_eq!(pane_map(&tree()).unwrap().len(), 2);
        let mut duplicate = tree();
        duplicate[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_pane"] = json!(A);
        assert!(pane_map(&duplicate).is_err());
        assert!(pane_map(&json!([])).is_err());
        let mut unmanaged = tree();
        unmanaged[0]["tabs"][0]["windows"][0]["user_vars"] = json!({});
        assert!(pane_map(&unmanaged).is_err());
    }

    #[test]
    fn focus_requires_unambiguous_active_windows() {
        let mut current = tree();
        current[0]["last_focused"] = json!(true);
        current[0]["tabs"][0]["is_active"] = json!(true);
        current[0]["tabs"][0]["windows"][1]["is_active"] = json!(true);
        assert_eq!(focused_pane(&current).unwrap(), B);
        current[0]["tabs"][0]["windows"][0]["is_active"] = json!(true);
        assert!(focused_pane(&current).is_err());
        assert_eq!(
            saved_focus(&format!("# kzw-focus {{\"pane\":\"{B}\"}}\n")).unwrap(),
            Some(B.into())
        );
        assert_eq!(saved_focus("new_tab Test\n").unwrap(), None);
        assert!(saved_focus("# kzw-focus {\"pane\":\"invalid\"}\n").is_err());
    }

    #[test]
    fn remote_translation_round_trips_without_client_paths() {
        let panes = pane_map(&tree()).unwrap();
        let snapshot = rewrite_export(
            "cd /private/path\nlaunch 'kitty-unserialize-data={\"id\":8}' --cwd=/private/path\n",
            &BTreeMap::from([(8, panes[&8].clone())]),
            "/private/kzw-pane",
        )
        .unwrap();
        let (portable, found) = translate(&snapshot, "", "dev.example", true).unwrap();
        assert_eq!(found, vec![B]);
        assert!(!portable.contains("/private"));
        assert!(portable.contains(EXECUTABLE));
        let (restored, found) =
            translate(&portable, "/other/kzw-pane", "dev.example", false).unwrap();
        assert_eq!(found, vec![B]);
        assert!(restored.contains("/other/kzw-pane"));
        assert!(!restored.contains(EXECUTABLE));
        assert!(translate(&snapshot, "", "wrong-host", true).is_err());
        assert!(translate(
            &format!("{portable}{portable}"),
            "/bin/kzw-pane",
            "dev.example",
            false
        )
        .is_err());
    }

    #[test]
    fn rewrites_launches_and_refuses_partial_layouts() {
        let panes = pane_map(&tree()).unwrap();
        let source = "new_tab Test\nlaunch 'kitty-unserialize-data={\"id\":7,\"cmd_at_shell_startup\":\"bad\"}' --var=kzw_staging=1 echo bad\nlaunch 'kitty-unserialize-data={\"id\":8}'\n";
        let output = rewrite_export(source, &panes, "/a path/kzw-pane").unwrap();
        assert!(!output.contains("cmd_at_shell_startup"));
        assert!(!output.contains("echo bad"));
        assert!(!output.contains("kzw_staging"));
        assert!(output.contains("'/a path/kzw-pane' --pane"));
        assert!(rewrite_export(
            "launch 'kitty-unserialize-data={\"id\":7}'\n",
            &panes,
            "/bin/kzw-pane"
        )
        .is_err());
        assert!(rewrite_export("launch --cwd=/tmp bash\n", &panes, "/bin/kzw-pane").is_err());
    }
}
