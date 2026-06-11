//! vyr-cli — the std shell. One-shot: IR JSON file in → PNG out, counters on
//! stdout. Mirrors the vyvanse-runner farm contract (env: scene path, W/H,
//! out path). If process-spawn cost ever dominates farm throughput, this
//! grows a persistent stdin loop mode — core unchanged.

use std::process::ExitCode;

fn main() -> ExitCode {
    // Pre-F1 skeleton: exercise the real entry point so the honest-failure
    // path is the first behaviour this binary ever has.
    let mut buf = [0u8; 4 * 3];
    let area = vyr_core::Rect { x: 0, y: 0, w: 2, h: 2 };
    match vyr_core::render("{}", area, &mut buf, 6) {
        Ok(_) => {
            eprintln!("vyr-cli: unexpected Ok from pre-F1 skeleton");
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("vyr-cli: {e:?}");
            eprintln!("vyr-cli: pre-F1 skeleton — see docs/plan.md");
            ExitCode::from(2)
        }
    }
}
