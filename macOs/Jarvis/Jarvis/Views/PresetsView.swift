import SwiftUI
import UniformTypeIdentifiers
import AppKit

struct PresetsView: View {
    @EnvironmentObject var state: AppState

    @State private var logContent = ""
    @State private var isAutoRefreshing = false
    @State private var refreshTimer: Timer?
    @State private var lastReadOffset: UInt64 = 0

    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".jarvis/jarvis.log")

    var body: some View {
        Form {
            Section {
                // Default config — always visible
                DefaultPresetRowView()

                ForEach(state.presets) { preset in
                    PresetRowView(preset: preset)
                }
            } header: {
                HStack {
                    Text("Config Presets")
                    Spacer()
                    Button {
                        pickPresetFile()
                    } label: {
                        Label("Add Preset", systemImage: "plus")
                    }
                    .buttonStyle(.borderless)
                    .disabled(!state.processManager.isRunning)
                    .help(state.processManager.isRunning
                          ? "Add a new preset"
                          : "Start the server to manage presets")
                }
            }

            Section("Active Config") {
                LabeledContent("File") {
                    Text(state.configURL.path(percentEncoded: false))
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
                LabeledContent("Servers") {
                    Text("\(state.servers.count) configured, \(state.servers.values.filter { $0.enabled ?? true }.count) enabled")
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                LogSectionView(logContent: logContent)
            } header: {
                HStack(spacing: 12) {
                    Text("Server Logs")
                    Spacer()
                    Toggle(isOn: $isAutoRefreshing) {
                        Label("Auto-refresh", systemImage: "arrow.clockwise")
                    }
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .help("Automatically refresh logs every second")
                    Button("Refresh") { loadLogs() }
                        .buttonStyle(.borderless)
                    Button("Clear", action: clearLogs)
                        .buttonStyle(.borderless)
                    Button("Open in Editor") { NSWorkspace.shared.open(logURL) }
                        .buttonStyle(.borderless)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Presets")
        .onAppear {
            loadLogs()
            startAutoRefresh()
            // Sync preset list from server if it's running
            if state.processManager.isRunning { state.fetchPresets() }
        }
        .onDisappear {
            isAutoRefreshing = false
            refreshTimer?.invalidate()
            refreshTimer = nil
        }
        .onChange(of: isAutoRefreshing) { _, newValue in
            if newValue { startAutoRefresh() }
        }
    }

    // MARK: - Log helpers

    private func loadLogs(force: Bool = false) {
        DispatchQueue.global(qos: .utility).async {
            guard let handle = try? FileHandle(forReadingFrom: logURL) else {
                DispatchQueue.main.async {
                    logContent = "No logs found at \(logURL.path(percentEncoded: false))\n\nThe log file will be created when the server starts."
                    lastReadOffset = 0
                }
                return
            }
            defer { try? handle.close() }

            let fileSize = (try? handle.seekToEnd()) ?? 0

            if force || lastReadOffset == 0 {
                let windowSize: UInt64 = 512 * 1024
                let startOffset = fileSize > windowSize ? fileSize - windowSize : 0
                try? handle.seek(toOffset: startOffset)
                let data = handle.readDataToEndOfFile()
                let raw = String(data: data, encoding: .utf8) ?? ""
                let lines = raw.split(separator: "\n", omittingEmptySubsequences: false)
                let trimmed = lines.suffix(10_000).joined(separator: "\n")
                DispatchQueue.main.async {
                    logContent = trimmed
                    lastReadOffset = fileSize
                }
            } else if fileSize > lastReadOffset {
                try? handle.seek(toOffset: lastReadOffset)
                let data = handle.readDataToEndOfFile()
                guard let newText = String(data: data, encoding: .utf8), !newText.isEmpty else { return }
                DispatchQueue.main.async {
                    logContent += newText
                    lastReadOffset = fileSize
                }
            }
        }
    }

    private func clearLogs() {
        try? "".write(to: logURL, atomically: true, encoding: .utf8)
        lastReadOffset = 0
        logContent = ""
        loadLogs(force: true)
    }

    private func startAutoRefresh() {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            guard isAutoRefreshing else {
                refreshTimer?.invalidate()
                refreshTimer = nil
                return
            }
            loadLogs()
        }
    }

    private func pickPresetFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        panel.showsHiddenFiles = true
        panel.message = "Select a servers.json config file"
        panel.prompt = "Add Preset"

        if panel.runModal() == .OK, let url = panel.url {
            let name = url.deletingPathExtension().lastPathComponent
            state.addPreset(name: name, filePath: url.path)
        }
    }
}

// MARK: - Default preset row

struct DefaultPresetRowView: View {
    @EnvironmentObject var state: AppState

    var isActive: Bool { state.activePresetID == nil }

    var body: some View {
        HStack(spacing: 10) {
            Button {
                if !isActive { state.switchPreset(nil) }
            } label: {
                Image(systemName: isActive ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isActive ? Color.accentColor : Color.secondary)
                    .font(.title3)
            }
            .buttonStyle(.plain)
            .disabled(!state.processManager.isRunning && !isActive)
            .help(isActive ? "Default config is active" : "Switch to default config")

            VStack(alignment: .leading, spacing: 2) {
                Text("Default")
                    .font(.body)
                let path = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".jarvis/servers.json").path
                Text((path as NSString).abbreviatingWithTildeInPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer()
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Preset row

struct PresetRowView: View {
    let preset: Preset
    @EnvironmentObject var state: AppState
    @State private var editingName: String = ""
    @State private var showDeleteConfirm = false

    var isActive: Bool { state.activePresetID == preset.id }

    var body: some View {
        HStack(spacing: 10) {
            Button {
                if !isActive { state.switchPreset(preset) }
            } label: {
                Image(systemName: isActive ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isActive ? Color.accentColor : Color.secondary)
                    .font(.title3)
            }
            .buttonStyle(.plain)
            .disabled(!state.processManager.isRunning && !isActive)
            .help(isActive ? "This preset is active" : "Switch to this preset")

            VStack(alignment: .leading, spacing: 2) {
                TextField("Preset name", text: $editingName)
                    .font(.body)
                    .textFieldStyle(.plain)
                    .disabled(!state.processManager.isRunning)
                    .onSubmit {
                        if state.processManager.isRunning {
                            state.renamePreset(id: preset.id, to: editingName)
                        }
                    }
                Text((preset.filePath as NSString).abbreviatingWithTildeInPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer()

            Button {
                showDeleteConfirm = true
            } label: {
                Image(systemName: "trash")
                    .foregroundStyle(.red.opacity(0.7))
            }
            .buttonStyle(.plain)
            .disabled(!state.processManager.isRunning)
            .help("Remove preset")
        }
        .padding(.vertical, 2)
        .onAppear { editingName = preset.name }
        .onChange(of: preset.name) { _, new in editingName = new }
        .onChange(of: state.processManager.isRunning) { _, isRunning in
            // Reset transient edits when server becomes unavailable
            if !isRunning { editingName = preset.name }
        }
        .confirmationDialog(
            isActive ? "Remove active preset?" : "Remove preset?",
            isPresented: $showDeleteConfirm,
            titleVisibility: .visible
        ) {
            Button("Remove\(isActive ? " and restart server" : "")", role: .destructive) {
                state.removePreset(preset)
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(isActive
                 ? "This will switch back to the default config and restart the server if it is running."
                 : "The preset will be removed. The config file on disk is not affected.")
        }
    }
}

// MARK: - Log section

struct LogSectionView: View {
    let logContent: String

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(logContent)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .textSelection(.enabled)
                Color.clear
                    .frame(height: 1)
                    .id("logBottom")
            }
            .frame(height: 300)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .onChange(of: logContent) { _, _ in
                proxy.scrollTo("logBottom", anchor: .bottom)
            }
        }
    }
}