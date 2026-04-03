import Foundation

struct Preset: Identifiable, Codable, Equatable {
    var id: UUID
    var name: String
    var filePath: String

    init(id: UUID = UUID(), name: String, filePath: String) {
        self.id = id
        self.name = name
        self.filePath = filePath
    }
}
