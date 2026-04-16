import Foundation
import Combine

/// Tails a log file using kqueue (DispatchSource) — fires on each write,
/// reads only the new bytes since the last offset, and appends to logContent.
@MainActor
final class LogTailer: ObservableObject {
    @Published var logContent: String = ""

    let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".jarvis/jarvis.log")

    private var fileHandle: FileHandle?
    private var source: DispatchSourceFileSystemObject?
    private var offset: UInt64 = 0

    // MARK: - Lifecycle

    func start() {
        reload()
        watchFile()
    }

    func stop() {
        source?.cancel()
        source = nil
        fileHandle?.closeFile()
        fileHandle = nil
    }

    // MARK: - Actions

    /// Read the last 10 000 lines from scratch and (re)start the watcher.
    func reload() {
        stop()
        guard let content = try? String(contentsOf: logURL, encoding: .utf8) else {
            logContent = "No logs found at \(logURL.path(percentEncoded: false))\n\nThe log file will be created when the server starts."
            watchFile()
            return
        }
        let lines = content.split(separator: "\n", omittingEmptySubsequences: false)
        logContent = lines.suffix(10_000).joined(separator: "\n")
        offset = UInt64(logContent.utf8.count)
        watchFile()
    }

    func clear() {
        stop()
        try? "".write(to: logURL, atomically: true, encoding: .utf8)
        logContent = ""
        offset = 0
        watchFile()
    }

    // MARK: - kqueue watcher

    private func watchFile() {
        let path = logURL.path
        let fd = open(path, O_EVTONLY)
        guard fd >= 0 else { return }

        let fh = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        fileHandle = fh

        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .delete, .rename],
            queue: .global(qos: .utility)
        )

        src.setEventHandler { [weak self] in
            guard let self else { return }
            let data = fh.readDataToEndOfFile()
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                DispatchQueue.main.async { self.reload() }
                return
            }
            DispatchQueue.main.async {
                self.logContent += text
                self.offset += UInt64(data.count)
            }
        }

        src.setCancelHandler { close(fd) }

        fh.seek(toFileOffset: offset)
        src.resume()
        source = src
    }
}
