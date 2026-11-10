use std::io::Write;
use std::process::{Command, Stdio};

use serde_json::{json, Value};

#[test]
fn transforms_only_complete_managed_snapshots() {
    let pane = "kzw-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let request = json!({
        "tree": [{"tabs": [{"windows": [{"id": 7, "user_vars": {"kzw_pane": pane}}]}]}],
        "export": "new_tab Test\nlaunch 'kitty-unserialize-data={\"id\":7}' echo unsafe\n",
        "executable": "/bin/kzw-pane"
    });
    let mut process = Command::new(env!("CARGO_BIN_EXE_kzw-snapshot"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    write!(process.stdin.take().unwrap(), "{request}").unwrap();
    let output = process.wait_with_output().unwrap();
    assert!(output.status.success());
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    let snapshot = result["snapshot"].as_str().unwrap();
    assert!(snapshot.contains(pane));
    assert!(snapshot.contains("--existing"));
    assert!(!snapshot.contains("echo unsafe"));

    let mut incomplete = request.clone();
    incomplete["export"] = json!("new_tab Test\n");
    let mut process = Command::new(env!("CARGO_BIN_EXE_kzw-snapshot"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    write!(process.stdin.take().unwrap(), "{incomplete}").unwrap();
    let output = process.wait_with_output().unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
}
