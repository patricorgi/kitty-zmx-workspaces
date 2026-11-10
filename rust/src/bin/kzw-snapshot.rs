//! Read-only snapshot transformation for migration testing.

use std::io::{self, Read};

use serde_json::{json, Value};

fn run() -> Result<Value, String> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())?;
    let request: Value = serde_json::from_str(&input).map_err(|e| e.to_string())?;
    let tree = request.get("tree").ok_or("missing tree")?;
    let export = request
        .get("export")
        .and_then(Value::as_str)
        .ok_or("missing export")?;
    let executable = request
        .get("executable")
        .and_then(Value::as_str)
        .ok_or("missing executable")?;
    if executable.is_empty() {
        return Err("empty executable".into());
    }
    let panes = kzw_core::pane_map(tree)?;
    let snapshot = kzw_core::rewrite_export(export, &panes, executable)?;
    Ok(json!({"snapshot": snapshot}))
}

fn main() {
    match run() {
        Ok(response) => println!("{response}"),
        Err(error) => {
            eprintln!("kzw-snapshot: {error}");
            std::process::exit(1);
        }
    }
}
