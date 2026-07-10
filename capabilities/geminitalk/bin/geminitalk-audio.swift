import AVFAudio
import Foundation

private let captureRate = 16_000.0
private let playbackRate = 24_000.0

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("geminitalk audio: \(message)\n".utf8))
    exit(1)
}

let engine = AVAudioEngine()
let player = AVAudioPlayerNode()
let input = engine.inputNode
_ = engine.outputNode

do {
    // Apple's Voice Processing I/O supplies acoustic echo cancellation while
    // keeping the microphone open, so real user barge-in still works.
    try input.setVoiceProcessingEnabled(true)
} catch {
    fail("could not enable macOS Voice Processing: \(error)")
}

guard
    let captureFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: captureRate,
        channels: 1,
        interleaved: true
    ),
    let playbackFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: playbackRate,
        channels: 1,
        interleaved: false
    )
else {
    fail("could not create PCM audio formats")
}

let inputFormat = input.outputFormat(forBus: 0)
guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
    fail("default microphone is unavailable")
}
guard let tapFormat = AVAudioFormat(
    standardFormatWithSampleRate: inputFormat.sampleRate,
    channels: 1
) else {
    fail("could not create mono microphone tap format")
}
guard let captureConverter = AVAudioConverter(from: tapFormat, to: captureFormat) else {
    fail("could not create microphone sample-rate converter")
}

let stdoutQueue = DispatchQueue(label: "geminitalk.audio.stdout")

input.installTap(onBus: 0, bufferSize: 960, format: tapFormat) { buffer, _ in
    let ratio = captureRate / tapFormat.sampleRate
    let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio) + 32)
    guard let converted = AVAudioPCMBuffer(
        pcmFormat: captureFormat,
        frameCapacity: capacity
    ) else { return }

    var suppliedInput = false
    var conversionError: NSError?
    let status = captureConverter.convert(to: converted, error: &conversionError) {
        _, outputStatus in
        if suppliedInput {
            outputStatus.pointee = .noDataNow
            return nil
        }
        suppliedInput = true
        outputStatus.pointee = .haveData
        return buffer
    }

    guard status != .error, converted.frameLength > 0 else { return }
    let audioBuffer = converted.audioBufferList.pointee.mBuffers
    guard let bytes = audioBuffer.mData, audioBuffer.mDataByteSize > 0 else { return }
    let data = Data(bytes: bytes, count: Int(audioBuffer.mDataByteSize))
    stdoutQueue.async {
        FileHandle.standardOutput.write(data)
    }
}

engine.attach(player)
let deviceOutputFormat = engine.outputNode.inputFormat(forBus: 0)
engine.connect(engine.mainMixerNode, to: engine.outputNode, format: deviceOutputFormat)
engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
engine.prepare()

do {
    try engine.start()
    player.play()
} catch {
    fail("could not start AVAudioEngine: \(error)")
}

FileHandle.standardError.write(
    Data("geminitalk audio: macOS Voice Processing enabled\n".utf8)
)

DispatchQueue.global(qos: .userInitiated).async {
    var pending = Data()
    while true {
        let incoming = FileHandle.standardInput.availableData
        if incoming.isEmpty { break }
        pending.append(incoming)

        // Framed stdin protocol: one command byte + big-endian payload size.
        // Command 0 streams a PCM packet; command 1 immediately discards the
        // currently scheduled playback after Gemini reports interruption.
        while pending.count >= 5 {
            let header = Array(pending.prefix(5))
            let byteCount = Int(header[1]) << 24
                | Int(header[2]) << 16
                | Int(header[3]) << 8
                | Int(header[4])
            if pending.count < 5 + byteCount { break }
            pending.removeFirst(5)

            if header[0] == 1 {
                if byteCount > 0 { pending.removeFirst(byteCount) }
                player.stop()
                player.reset()
                player.play()
                continue
            }

            let playable = pending.prefix(byteCount)
            pending.removeFirst(byteCount)
            guard byteCount > 0, byteCount % 2 == 0 else { continue }
            let frames = AVAudioFrameCount(byteCount / 2)

            guard let audio = AVAudioPCMBuffer(
                pcmFormat: playbackFormat,
                frameCapacity: frames
            ) else { continue }
            audio.frameLength = frames
            guard let destination = audio.floatChannelData?.pointee else { continue }
            playable.withUnsafeBytes { source in
                let samples = source.bindMemory(to: Int16.self)
                for index in 0..<samples.count {
                    destination[index] = Float(samples[index]) / 32_768.0
                }
            }
            player.scheduleBuffer(audio)
        }
    }

    engine.stop()
    exit(0)
}

dispatchMain()
