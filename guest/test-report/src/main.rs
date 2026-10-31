// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Export an already completed test command. Re-reading never executes it again.
use std::io::{Read, Write};

const LIMIT: usize = 1024 * 1024;

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let [token, base, command] = args.as_slice() else {
        return Err("usage: test-report TRANSFER_TOKEN FILE_PREFIX COMMAND_TOKEN".into());
    };
    if [token, command]
        .iter()
        .any(|s| s.len() != 32 || !s.bytes().all(|c| c.is_ascii_hexdigit()))
    {
        return Err("invalid report token".into());
    }
    let mut status = String::new();
    std::fs::File::open(format!("{base}.status"))?
        .take(65)
        .read_to_string(&mut status)?;
    if status.len() > 64 || !status.ends_with('\n') {
        return Err("invalid command status".into());
    }
    let mut fields = status.split_whitespace();
    if fields.next() != Some(command.as_str()) {
        return Err("missing or stale command status".into());
    }
    let status = fields
        .next()
        .ok_or("missing command status")?
        .parse::<i32>()?;
    if fields.next().is_some() {
        return Err("invalid command status".into());
    }
    let mut data = Vec::new();
    std::fs::File::open(format!("{base}.output"))?
        .take((LIMIT + 1) as u64)
        .read_to_end(&mut data)?;
    if data.len() > LIMIT {
        return Err("test output exceeds 1 MiB".into());
    }
    // FNV-1a detects accidental serial corruption; this is not authentication.
    let checksum = status
        .to_le_bytes()
        .into_iter()
        .chain(data.iter().copied())
        .fold(0xcbf2_9ce4_8422_2325u64, |hash, byte| {
            (hash ^ u64::from(byte)).wrapping_mul(0x100_0000_01b3)
        });
    let mut output = std::io::stdout().lock();
    for (index, chunk) in data.chunks(192).enumerate() {
        let mut line = format!("\nHTR1 {token} DATA {index} ");
        for byte in chunk {
            use std::fmt::Write;
            write!(line, "{byte:02x}")?;
        }
        line.push('\n');
        output.write_all(line.as_bytes())?;
    }
    let footer = format!(
        "\nHTR1 {token} END {status} {} {checksum:016x}\n",
        data.len()
    );
    output.write_all(footer.as_bytes())?;
    output.flush()?;
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("test-report: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
