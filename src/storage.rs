use crate::catalog::{cancel, fold};
use anyhow::{Context, Result, anyhow, ensure};
use serde::{Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    env,
    fs::{self, File, OpenOptions},
    io::{self, BufReader, Read, Write},
    path::{Component, Path, PathBuf},
    sync::{LazyLock, Mutex},
    thread,
    time::{Duration, UNIX_EPOCH},
};
use tokio_util::sync::CancellationToken;

pub fn user_home() -> Result<PathBuf> {
    env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .context("Could not find your home folder.")
}
pub fn expand(path: &Path) -> Result<PathBuf> {
    if let Ok(rest) = path.strip_prefix("~") {
        return Ok(user_home()?.join(rest));
    }
    Ok(path.to_owned())
}
pub fn home_dir(override_dir: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = override_dir {
        return expand(path);
    }
    if let Some(value) = ["IBFETCH_HOME", "IBFETCH_RUST_HOME"]
        .iter()
        .find_map(|key| env::var_os(key).filter(|s| !s.is_empty()))
    {
        return expand(Path::new(&value));
    }
    let base = if cfg!(windows) {
        match env::var_os("APPDATA") {
            Some(path) => PathBuf::from(path),
            None => user_home()?.join("AppData/Roaming"),
        }
    } else if cfg!(target_os = "macos") {
        user_home()?.join("Library/Application Support")
    } else {
        match env::var_os("XDG_CONFIG_HOME") {
            Some(path) => PathBuf::from(path),
            None => user_home()?.join(".config"),
        }
    };
    Ok(base.join(crate::APP_SLUG))
}
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<Option<T>> {
    match File::open(path) {
        Ok(file) => Ok(Some(serde_json::from_reader(BufReader::new(file))?)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}
pub fn create_private_dir(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}
pub fn write_private(path: &Path, write: impl FnOnce(&mut File) -> Result<()>) -> Result<()> {
    let folder = path.parent().context("File has no parent folder.")?;
    create_private_dir(folder)?;
    let mut file = tempfile::NamedTempFile::new_in(folder)?;
    write(file.as_file_mut())?;
    file.as_file_mut().flush()?;
    file.as_file().sync_all()?;
    let mut staged = file;
    for attempt in 0..6 {
        match staged.persist(path) {
            Ok(_) => return Ok(()),
            Err(error)
                if cfg!(windows)
                    && attempt < 5
                    && matches!(error.error.raw_os_error(), Some(5 | 32 | 33)) =>
            {
                staged = error.file;
                thread::sleep(Duration::from_millis(30 * (attempt + 1)));
            }
            Err(error) => return Err(error.error.into()),
        }
    }
    unreachable!()
}
pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    write_private(path, |f| Ok(serde_json::to_writer(f, value)?))
}
pub fn remove_file(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}
pub fn exists(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(md) if md.file_type().is_symlink() => Err(io::Error::other(
            "Symbolic link at a download or record path; preserved.",
        )),
        Ok(_) => Ok(true),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}
pub fn canonical(path: &Path) -> Result<PathBuf> {
    let absolute = std::path::absolute(path)?;
    let mut remainder = Vec::new();
    let mut ancestor = absolute.as_path();
    loop {
        match dunce_canonical(ancestor) {
            Ok(mut root) => {
                for c in remainder.iter().rev().copied() {
                    if c == ".." {
                        root.pop();
                    } else if c != "." {
                        root.push(c);
                    }
                }
                return Ok(root);
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                remainder.push(match ancestor.components().next_back() {
                    Some(Component::Normal(name)) => name,
                    Some(Component::ParentDir) => std::ffi::OsStr::new(".."),
                    _ => return Err(e.into()),
                });
                ancestor = ancestor.parent().context("Could not resolve folder.")?;
            }
            Err(e) => return Err(e.into()),
        }
    }
}
fn dunce_canonical(path: &Path) -> io::Result<PathBuf> {
    let path = fs::canonicalize(path)?;
    #[cfg(windows)]
    {
        // Preserve the ordinary Python path spelling when hashing shared lock names.
        let value = path.to_string_lossy();
        if let Some(unc) = value.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{unc}")));
        }
        if let Some(drive) = value.strip_prefix(r"\\?\")
            && drive.as_bytes().get(1) == Some(&b':')
        {
            return Ok(PathBuf::from(drive));
        }
    }
    Ok(path)
}
pub fn path_digest(path: &Path) -> Result<String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(fold(&canonical(path)?.to_string_lossy()).as_bytes())
    ))
}
fn open_lock(path: &Path) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    opts.open(path)
}
#[cfg(not(windows))]
fn try_lock(file: &File) -> io::Result<bool> {
    use std::fs::TryLockError;
    match file.try_lock() {
        Ok(()) => Ok(true),
        Err(TryLockError::WouldBlock) => Ok(false),
        Err(TryLockError::Error(e)) => Err(e),
    }
}
#[cfg(windows)]
fn try_lock(file: &File) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::ERROR_LOCK_VIOLATION,
        Storage::FileSystem::{LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx},
        System::IO::OVERLAPPED,
    };
    // Python uses msvcrt.locking over byte zero. Use the same byte range here.
    let mut offset = OVERLAPPED::default();
    let ok = unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut offset,
        )
    };
    if ok != 0 {
        return Ok(true);
    }
    let e = io::Error::last_os_error();
    if e.raw_os_error() == Some(ERROR_LOCK_VIOLATION as i32) {
        Ok(false)
    } else {
        Err(e)
    }
}
static CHECKED: LazyLock<Mutex<HashSet<PathBuf>>> = LazyLock::new(|| Mutex::new(HashSet::new()));
fn check_locks(directory: &Path) -> Result<()> {
    let directory = canonical(directory)?;
    let mut checked = CHECKED
        .lock()
        .map_err(|_| anyhow!("Lock support unavailable."))?;
    if !checked.contains(&directory) {
        let name = tempfile::Builder::new()
            .prefix(".lock-test-")
            .tempfile_in(&directory)?;
        let first = open_lock(name.path())?;
        let second = open_lock(name.path())?;
        ensure!(
            try_lock(&first)? && !try_lock(&second)?,
            "The ibfetch settings folder must be on a local filesystem that enforces locks."
        );
        checked.insert(directory);
    }
    Ok(())
}
pub fn target_lock(folder: &Path, path: &Path, stop: &CancellationToken) -> Result<File> {
    create_private_dir(folder)?;
    check_locks(folder)?;
    let name = format!("slot-{}.lock", &path_digest(path)?[..3]);
    let lock = open_lock(&folder.join(name))?;
    loop {
        cancel(stop)?;
        if try_lock(&lock)? {
            return Ok(lock);
        }
        thread::sleep(Duration::from_millis(100));
    }
}
#[derive(Clone, Copy, Eq, PartialEq)]
pub struct Baseline {
    pub size: u64,
    pub mtime_ns: i64,
}
pub fn baseline(path: &Path) -> Result<Baseline> {
    let meta = fs::symlink_metadata(path)?;
    ensure!(meta.is_file(), "Target is not a regular file; preserved.");
    let duration = meta.modified()?.duration_since(UNIX_EPOCH);
    let nanos = match duration {
        Ok(d) => d.as_nanos() as i128,
        Err(e) => -(e.duration().as_nanos() as i128),
    };
    Ok(Baseline {
        size: meta.len(),
        mtime_ns: i64::try_from(nanos)?,
    })
}
pub fn hasher(path: &Path, stop: &CancellationToken) -> Result<Sha256> {
    let mut hash = Sha256::new();
    let mut f = BufReader::new(File::open(path)?);
    let mut buf = vec![0; 256 * 1024];
    loop {
        cancel(stop)?;
        let n = f.read(&mut buf)?;
        if n == 0 {
            return Ok(hash);
        }
        hash.update(&buf[..n]);
    }
}
pub fn file_hash(path: &Path, stop: &CancellationToken) -> Result<String> {
    Ok(format!("{:x}", hasher(path, stop)?.finalize()))
}
pub fn publish_audio(source: &Path, destination: &Path) -> Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::MoveFileW;
        let from: Vec<u16> = source.as_os_str().encode_wide().chain([0]).collect();
        let to: Vec<u16> = destination.as_os_str().encode_wide().chain([0]).collect();
        if unsafe { MoveFileW(from.as_ptr(), to.as_ptr()) } == 0 {
            return Err(io::Error::last_os_error().into());
        }
    }
    #[cfg(unix)]
    {
        // Use an atomic no-replace rename where available. Hard linking is the
        // fallback on other Unix targets and never clobbers an existing file.
        match rename_exclusive(source, destination) {
            Ok(true) => return Ok(()),
            Ok(false) => {}
            Err(error) => return Err(error.into()),
        }
        fs::hard_link(source, destination)?;
        fs::remove_file(source)?;
    }
    #[cfg(not(any(unix, windows)))]
    anyhow::bail!("Publishing audio is unsupported on this platform.");
    Ok(())
}
#[cfg(unix)]
fn rename_exclusive(source: &Path, target: &Path) -> io::Result<bool> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};
    let from = CString::new(source.as_os_str().as_bytes())?;
    let to = CString::new(target.as_os_str().as_bytes())?;
    #[cfg(target_os = "macos")]
    let code = unsafe { libc::renamex_np(from.as_ptr(), to.as_ptr(), libc::RENAME_EXCL) };
    #[cfg(target_os = "linux")]
    let code = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            libc::AT_FDCWD,
            from.as_ptr(),
            libc::AT_FDCWD,
            to.as_ptr(),
            1u32,
        )
    };
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let code = {
        let _ = (from, to);
        return Ok(false);
    };
    if code == 0 {
        return Ok(true);
    }
    let e = io::Error::last_os_error();
    if matches!(
        e.raw_os_error(),
        Some(libc::ENOSYS | libc::ENOTSUP | libc::EINVAL)
    ) {
        Ok(false)
    } else {
        Err(e)
    }
}
pub fn hide_manifest(path: &Path) {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_HIDDEN, GetFileAttributesW, INVALID_FILE_ATTRIBUTES, SetFileAttributesW,
        };
        let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        unsafe {
            let attributes = GetFileAttributesW(path.as_ptr());
            if attributes != INVALID_FILE_ATTRIBUTES {
                SetFileAttributesW(path.as_ptr(), attributes | FILE_ATTRIBUTE_HIDDEN);
            }
        }
    }
    #[cfg(not(windows))]
    let _ = path;
}
