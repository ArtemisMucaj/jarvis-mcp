import Foundation
import Combine
import UserNotifications

class ProcessManager: ObservableObject {
    @Published var isRunning = false
    @Published var isStarting = false
    @Published var lastError: String?

    private var process: Process?
    private var processSource: DispatchSourceProcess?
    let port: Int

    init(port: Int = 7070) {
        self.port = port
        
        // Request notification permission
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            if granted {
                print("✓ Notification permission granted")
            }
        }
    }

    var endpoint: String { "http://127.0.0.1:\(port)/mcp" }

    // MARK: - Lifecycle

    func startBundled() {
        guard !isRunning && !isStarting else {
            print("⚠️ Already running or starting - ignoring start request")
            return
        }
        DispatchQueue.main.async { self.lastError = nil }

        guard let resourcePath = Bundle.main.resourcePath else {
            let msg = "Could not locate app bundle resources."
            DispatchQueue.main.async { self.lastError = msg }
            print("❌ \(msg)")
            return
        }

        let binaryPath = (resourcePath as NSString).appendingPathComponent("jarvis")
        let fileManager = FileManager.default

        guard fileManager.isExecutableFile(atPath: binaryPath) else {
            let msg = "Bundled jarvis binary not found at: \(binaryPath)\n\nRebuild the app after running scripts/build_jarvis_binary.sh"
            DispatchQueue.main.async { self.lastError = msg }
            print("❌ \(msg)")
            return
        }

        print("🔄 Setting isStarting = true (bundled binary)")
        print("📦 Binary: \(binaryPath)")
        DispatchQueue.main.async { self.isStarting = true }

        let logURL = logFileURL()
        prepareLogFile(at: logURL)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binaryPath)
        proc.arguments = ["--http", "\(port)"]
        proc.currentDirectoryURL = fileManager.homeDirectoryForCurrentUser
        proc.environment = Self.shellEnvironment
        proc.standardOutput = logHandle(for: logURL)
        proc.standardError  = logHandle(for: logURL)

        do {
            try proc.run()
            process = proc
            print("✓ Jarvis MCP process launched from bundled binary")
            DispatchQueue.main.async { self.markRunning() }
        } catch {
            let msg = error.localizedDescription
            DispatchQueue.main.async {
                self.isStarting = false
                self.lastError = msg
            }
            print("❌ Failed to start bundled jarvis: \(error)")
        }
    }

    func stop() {
        processSource?.cancel()
        processSource = nil
        if let proc = process, proc.isRunning {
            // Kill the entire process group so child processes don't linger
            let pgid = proc.processIdentifier
            kill(-pgid, SIGTERM)
            proc.terminate()
        }
        process = nil
        isRunning = false
        isStarting = false
    }

    // MARK: - Private

    private func markStopped() {
        process = nil
        isRunning = false
        isStarting = false
        processSource?.cancel()
        processSource = nil
    }

    private func markRunning() {
        isStarting = false
        isRunning = true
        if let pid = process?.processIdentifier { watchProcess(pid) }
        print("✅ Jarvis MCP server is ready on port \(port)")
        
        // Show notification
        showReadyNotification()
    }
    
    private func showReadyNotification() {
        let content = UNMutableNotificationContent()
        content.title = "Jarvis MCP Server Ready"
        content.body = "Server is running on \(endpoint)"
        content.sound = .default
        
        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        
        UNUserNotificationCenter.current().add(request)
    }
    
    private func watchProcess(_ pid: pid_t) {
        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .global(qos: .utility))
        source.setEventHandler { [weak self] in
            DispatchQueue.main.async { self?.markStopped() }
        }
        source.resume()
        processSource = source
    }

    private func logFileURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis/jarvis.log")
    }

    private func prepareLogFile(at url: URL) {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
    }

    private func logHandle(for url: URL) -> FileHandle? {
        try? FileHandle(forWritingTo: url)
    }

    // MARK: - Shell environment

    /// Capture the user's login shell PATH so child processes can find npx, terraform, etc.
    /// macOS GUI apps inherit a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin).
    private static let shellEnvironment: [String: String] = {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-l", "-c", "env"]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()

            if process.terminationStatus == 0 {
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                if let output = String(data: data, encoding: .utf8) {
                    var env: [String: String] = [:]
                    for line in output.components(separatedBy: "\n") {
                        guard let eqIdx = line.firstIndex(of: "=") else { continue }
                        let key = String(line[line.startIndex..<eqIdx])
                        let value = String(line[line.index(after: eqIdx)...])
                        env[key] = value
                    }
                    if !env.isEmpty {
                        print("✓ Captured shell environment (\(env.count) vars)")
                        return env
                    }
                }
            }
        } catch {
            print("⚠️ Failed to capture shell environment: \(error)")
        }

        // Fallback: use current process env (minimal but better than nothing)
        return ProcessInfo.processInfo.environment
    }()

}
