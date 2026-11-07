// Built as a facelock-tpm binary against the same pinned upstream format.
use facelock_core::{Config, types::{cosine_similarity, FaceEmbedding}};
use facelock_tpm::{SoftwareSealer, TpmSealer};
use std::{error::Error, io::{self, Read}, path::Path};
use zeroize::Zeroizing;

fn read_pose(input: &mut impl Read, sealer: &SoftwareSealer, aad: Option<&[u8]>)
    -> Result<Zeroizing<FaceEmbedding>, Box<dyn Error>> {
    // Facelock AES-GCM version + nonce + 512 f32s + authentication tag.
    let mut sealed = [0u8; 2077];
    input.read_exact(&mut sealed)?;
    if sealed[0] != 2 { return Err("unsupported encryption".into()); }
    Ok(Zeroizing::new(sealer.unseal_embedding_with_aad(&sealed, aad)?))
}

fn verify() -> Result<(), Box<dyn Error>> {
    if unsafe { libc::geteuid() } != 0 { return Err("requires root".into()); }
    unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0); }
    let device = std::env::args().nth(1).ok_or("device required")?;
    let config = Config::load_from(Path::new("/etc/facelock/config.toml"))?;
    let aad = config.require_device_aad(Some(&device)).map_err(|_| "device binding failed")?;
    let mut tpm = TpmSealer::new(&config.tpm.tcti)?;
    let key = Zeroizing::new(tpm.unseal_key_from_file(Path::new(&config.encryption.sealed_key_path))?);
    let sealer = SoftwareSealer::from_key(*key);
    let mut input = io::stdin().lock();
    let mut originals = Vec::with_capacity(6);
    for _ in 0..6 { originals.push(read_pose(&mut input, &sealer, aad.as_deref())?); }
    for _ in 0..4 {
        let sample = read_pose(&mut input, &sealer, aad.as_deref())?;
        let matches = originals.iter().filter(|anchor|
            cosine_similarity(anchor, &sample) >= config.recognition.threshold.max(0.8)).count();
        if matches < 2 { return Err("pose mismatch".into()); }
    }
    if input.bytes().next().is_some() { return Err("unexpected input".into()); }
    Ok(())
}

fn main() {
    std::process::exit(if verify().is_ok() { 0 } else { 1 });
}
