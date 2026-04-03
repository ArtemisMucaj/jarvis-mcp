import SwiftUI
import UniformTypeIdentifiers

struct PresetsView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Form {
            Section {
                if state.presets.isEmpty {
                    Text("No presets added yet.")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, 8)
                } else {
                    ForEach($state.presets) { $preset in
                        PresetRowView(preset: $preset)
                    }
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
        }
        .formStyle(.grouped)
        .navigationTitle("Presets")
    }

    private func pickPresetFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        panel.message = "Select a servers.json config file"
        panel.prompt = "Add Preset"

        if panel.runModal() == .OK, let url = panel.url {
            let name = url.deletingPathExtension().lastPathComponent
            state.addPreset(name: name, filePath: url.path)
        }
    }
}

struct PresetRowView: View {
    @Binding var preset: Preset
    @EnvironmentObject var state: AppState
    @State private var showDeleteConfirm = false

    var isActive: Bool { state.activePresetID == preset.id }

    var body: some View {
        HStack(spacing: 10) {
            Button {
                state.switchPreset(isActive ? nil : preset)
            } label: {
                Image(systemName: isActive ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isActive ? Color.accentColor : Color.secondary)
                    .font(.title3)
            }
            .buttonStyle(.plain)
            .help(isActive ? "Deactivate preset (use default)" : "Switch to this preset")

            VStack(alignment: .leading, spacing: 2) {
                TextField("Preset name", text: $preset.name)
                    .font(.body)
                    .textFieldStyle(.plain)
                Text((preset.filePath as NSString).abbreviatingWithTildeInPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer()

            Button(role: .destructive) {
                showDeleteConfirm = true
            } label: {
                Image(systemName: "trash")
                    .foregroundStyle(.red.opacity(0.7))
            }
            .buttonStyle(.plain)
            .help("Remove preset")
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
        .padding(.vertical, 2)
    }
}
