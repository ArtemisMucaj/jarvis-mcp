import SwiftUI
import UniformTypeIdentifiers
import AppKit

struct PresetsView: View {
    @EnvironmentObject var state: AppState

    @StateObject private var tailer = LogTailer()

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
                let allTools = state.discoveredTools.values.flatMap { $0 }
                let totalTools = allTools.count
                let enabledTools = state.discoveredTools.reduce(0) { count, entry in
                    let (serverName, tools) = entry
                    return count + tools.filter { !state.isToolDisabled(server: serverName, tool: $0.name) }.count
                }
                LabeledContent("Tools") {
                    Text("\(enabledTools)/\(totalTools) enabled")
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                LogSectionView(logContent: tailer.logContent)
            } header: {
                HStack(spacing: 12) {
                    Text("Server Logs")
                    Spacer()
                    Button("Clear", action: tailer.clear)
                        .buttonStyle(.borderless)
                    Button("Open in Editor") { NSWorkspace.shared.open(tailer.logURL) }
                        .buttonStyle(.borderless)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Presets")
        .onAppear {
            tailer.start()
            // Sync preset list from server if it's running
            if state.processManager.isRunning { state.fetchPresets() }
        }
        .onDisappear {
            tailer.stop()
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