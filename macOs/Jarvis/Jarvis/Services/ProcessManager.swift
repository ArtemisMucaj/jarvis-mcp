import Foundation
import Combine
import UserNotifications

class ProcessManager: ObservableObject {
    @Published var isRunning = false
    @Published var isStarting = false
    @Published var lastError: String?

    private var process: Process?
    private var pollTimer: Timer?
    private var healthCheckTimer: Timer?
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
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.markStopped() }
        }

        do {
            try proc.run()
            process = proc
            print("✓ Jarvis MCP process launched from bundled binary")
            DispatchQueue.main.async { self.startHealthCheck() }

            // 15s timeout — bundled binary starts in <2s
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
                guard let self, self.isStarting else { return }
                print("❌ Startup timeout after 15 seconds")
                self.isStarting = false
                self.lastError = "Server startup timeout — check logs at \(logURL.path)"
                self.stop()
            }
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
        pollTimer?.invalidate()
        pollTimer = nil
        healthCheckTimer?.invalidate()
        healthCheckTimer = nil
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
        pollTimer?.invalidate()
        pollTimer = nil
        healthCheckTimer?.invalidate()
        healthCheckTimer = nil
    }
    
    private func markRunning() {
        print("🎉 markRunning() called - setting isRunning=true, isStarting=false")
        isStarting = false
        isRunning = true
        healthCheckTimer?.invalidate()
        healthCheckTimer = nil
        startPolling()
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
    
    private func startHealthCheck() {
        print("🏥 Starting health check timer...")
        var attempts = 0
        healthCheckTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] timer in
            guard let self else {
                print("⚠️ Health check timer fired but self is nil")
                timer.invalidate()
                return
            }
            
            attempts += 1
            if attempts == 1 {
                print("🏥 First health check starting...")
            }
            if attempts % 5 == 0 {
                print("⏳ Health check attempt \(attempts)... (isStarting: \(self.isStarting))")
            }
            
            self.checkServerHealth { isHealthy in
                if isHealthy {
                    print("✅ Health check succeeded!")
                    DispatchQueue.main.async {
                        timer.invalidate()
                        self.markRunning()
                    }
                } else if attempts == 1 {
                    print("⏳ Server not ready yet, will keep checking...")
                }
            }
        }
    }
    
    private func checkServerHealth(completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: endpoint) else {
            print("❌ Invalid endpoint URL: \(endpoint)")
            completion(false)
            return
        }
        
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 2.0
        
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                // Only log first error
                if let nsError = error as NSError?, nsError.code != NSURLErrorTimedOut {
                    print("🔍 Health check error: \(error.localizedDescription)")
                }
                completion(false)
            } else if let httpResponse = response as? HTTPURLResponse {
                print("✅ Health check got response: HTTP \(httpResponse.statusCode)")
                // Accept any response (200, 404, etc.) - just means server is up
                completion(httpResponse.statusCode > 0)
            } else {
                completion(false)
            }
        }
        task.resume()
    }

    private func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self, let proc = self.process else { return }
            if !proc.isRunning { DispatchQueue.main.async { self.markStopped() } }
        }
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

    // MARK: - uv detection

    static func detectUVPath() -> String {
        // Try multiple detection methods
        
        // Method 1: Use shell to find uv (this respects PATH from .zshrc, .bashrc, etc.)
        if let shellPath = detectViaShell() {
            print("✓ Found UV via shell: \(shellPath)")
            return shellPath
        }
        
        // Method 2: Use 'which' command
        if let whichPath = detectViaWhich() {
            print("✓ Found UV via which: \(whichPath)")
            return whichPath
        }
        
        // Method 3: Check common installation locations
        let candidates = [
            "/opt/homebrew/bin/uv",                     // Apple Silicon Homebrew
            "/usr/local/bin/uv",                        // Intel Homebrew
            "\(NSHomeDirectory())/.local/bin/uv",       // pip/pipx/uv default
            "\(NSHomeDirectory())/.cargo/bin/uv",       // Rust cargo install
            "\(NSHomeDirectory())/Library/Python/3.*/bin/uv", // Python user install
            "/usr/bin/uv",
        ]
        
        for pattern in candidates {
            // Handle glob patterns
            if pattern.contains("*") {
                if let found = findFirstMatch(pattern: pattern) {
                    print("✓ Found UV via pattern match: \(found)")
                    return found
                }
            } else if FileManager.default.isExecutableFile(atPath: pattern) {
                print("✓ Found UV at common location: \(pattern)")
                return pattern
            }
        }
        
        print("⚠️ UV not found, returning default path")
        return "/opt/homebrew/bin/uv"
    }
    
    private static func detectViaShell() -> String? {
        // Use the user's shell to find uv (this will load .zshrc, .bashrc, etc.)
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-l", "-c", "which uv"]  // -l for login shell (loads configs)
        
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        
        do {
            try process.run()
            process.waitUntilExit()
            
            if process.terminationStatus == 0 {
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                if let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !path.isEmpty,
                   FileManager.default.isExecutableFile(atPath: path) {
                    return path
                }
            }
        } catch {
            print("Failed to run shell detection: \(error)")
        }
        
        return nil
    }
    
    private static func detectViaWhich() -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = ["uv"]
        
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        
        do {
            try process.run()
            process.waitUntilExit()
            
            if process.terminationStatus == 0 {
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                if let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !path.isEmpty,
                   FileManager.default.isExecutableFile(atPath: path) {
                    return path
                }
            }
        } catch {
            print("Failed to run 'which uv': \(error)")
        }
        
        return nil
    }
    
    private static func findFirstMatch(pattern: String) -> String? {
        // Simple glob pattern matching for paths like ~/.local/Python/3.*/bin/uv
        let components = pattern.components(separatedBy: "*")
        guard components.count == 2 else { return nil }
        
        let baseDir = (components[0] as NSString).deletingLastPathComponent
        let remainder = (components[0] as NSString).lastPathComponent + components[1]
        
        guard let enumerator = FileManager.default.enumerator(atPath: baseDir) else { return nil }
        
        for case let file as String in enumerator {
            let fullPath = (baseDir as NSString).appendingPathComponent(file)
            if file.hasSuffix(remainder.replacingOccurrences(of: components[0], with: "")),
               FileManager.default.isExecutableFile(atPath: fullPath) {
                return fullPath
            }
        }
        
        return nil
    }
}
