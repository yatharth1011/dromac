// Plays the phone's audio after Dromac's effects (8D, slowed + reverb):
// raw PCM (s16le, 48 kHz, stereo) on stdin, out through this Mac's current
// output device -- built-in speakers, AirPods, Bluetooth headphones, AirPlay or
// a Multi-Output Device -- following it when the output changes.
// Built by server.py on first use: swiftc -O dromac_audio.swift -o dromac-audio
//
// argv[1] = buffers of 20 ms to keep queued (default 4 = 80 ms). Playback only
// starts once that many are queued, so a jittery link (Wi-Fi) has a cushion
// instead of running dry -- running dry is what makes the clicks.
// argv[2] = "headphones only": the output device this must play on (its name).
// If macOS moves the output anywhere else -- the headphones dropped, and the
// Mac fell back to its own speakers -- this goes silent and exits with code 3
// instead of following, so nothing ever plays out loud.
// `dromac-audio --current-output` just prints the current output's name.
// `dromac-audio --connect NAME` connects this Mac to the paired Bluetooth
// device NAME (e.g. the headphones) and prints "connected" or "failed".
// While playing, it exits with code 4 when the Mac is about to sleep, so the
// server can hand the audio back to the phone before the lid shuts.
import AppKit
import AVFoundation
import CoreAudio
import Foundation
import IOBluetooth
import MediaPlayer

func currentOutputName() -> String {
    var device = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr else { return "" }
    var name: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    address.mSelector = kAudioObjectPropertyName
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name) == noErr, let n = name else { return "" }
    return n.takeRetainedValue() as String
}

if CommandLine.arguments.dropFirst().first == "--current-output" {
    print(currentOutputName())
    exit(0)
}
if CommandLine.arguments.dropFirst().first == "--connect", CommandLine.arguments.count > 2 {
    let name = CommandLine.arguments[2]
    let paired = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
    guard let device = paired.first(where: { $0.name == name }) else { print("not paired"); exit(2) }
    if device.isConnected() { print("connected"); exit(0) }
    print(device.openConnection() == kIOReturnSuccess ? "connected" : "failed")
    exit(0)
}
let lockedTo = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : ""

// How long the output takes, for head tracking's prediction (dromac_fx.py):
// what the device reports, and whether it's Bluetooth -- whose link adds
// ~150 ms more that macOS doesn't count.
func writeOutputLatency() {
    var device = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                       mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &device) == noErr else { return }
    func u32(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope) -> UInt32 {
        var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        var v = UInt32(0); var n = UInt32(MemoryLayout<UInt32>.size)
        AudioObjectGetPropertyData(device, &addr, 0, nil, &n, &v); return v
    }
    var rate = Float64(48000); var n = UInt32(MemoryLayout<Float64>.size)
    var ra = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
                                        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    AudioObjectGetPropertyData(device, &ra, 0, nil, &n, &rate)
    let frames = u32(kAudioDevicePropertyLatency, kAudioObjectPropertyScopeOutput)
        + u32(kAudioDevicePropertySafetyOffset, kAudioObjectPropertyScopeOutput)
        + u32(kAudioDevicePropertyBufferFrameSize, kAudioObjectPropertyScopeGlobal)
    let bluetooth = [kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE]
        .contains(u32(kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal))
    let json = "{\"device_ms\": \(Double(frames) / rate * 1000), \"bluetooth\": \(bluetooth)}"
    let url = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().appendingPathComponent("output_latency.json")
    try? json.write(to: url, atomically: true, encoding: .utf8)
}

func leaveIfNotLockedOutput() {
    guard !lockedTo.isEmpty else { return }
    let now = currentOutputName()
    if now != lockedTo {
        FileHandle.standardError.write("dromac-audio: output moved to \(now), not \(lockedTo); stopping\n".data(using: .utf8)!)
        exit(3)
    }
}
leaveIfNotLockedOutput()

let sampleRate = 48000.0
let channels: AVAudioChannelCount = 2
let framesPerBuffer = 960
let bytesPerBuffer = framesPerBuffer * Int(channels) * 2
let maxQueued = max(2, Int(CommandLine.arguments.dropFirst().first ?? "") ?? 4)   // the start-up cushion
let queueCapacity = 30   // the most player_cfg.json may ask for (600 ms: ultramusemax)

let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
                           channels: channels, interleaved: false)!
var engine = AVAudioEngine()
var player = AVAudioPlayerNode()
let audioLock = NSLock()  // playback thread vs. rebuilds
var configObserver: NSObjectProtocol?
var playbackStarted = false  // set once the start-up cushion is queued

// Builds the engine and player from scratch on the current output. Used at
// start and after every configuration change: Bluetooth headphones change
// format whenever they switch codec, sample rate or into headset mode (the
// mic gets used), and merely restarting the old engine can leave the player
// "playing" -- taking audio as fast as it comes -- into nothing. Silence.
func buildEngine() {
    if let o = configObserver { NotificationCenter.default.removeObserver(o) }
    let e = AVAudioEngine()
    let p = AVAudioPlayerNode()
    e.attach(p)
    e.connect(p, to: e.mainMixerNode, format: format)
    e.prepare()
    do {
        try e.start()
    } catch {
        FileHandle.standardError.write("dromac-audio: can't start audio output: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
    if playbackStarted { p.play() }
    engine = e
    player = p
    writeOutputLatency()
    configObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: e, queue: nil) { _ in
        leaveIfNotLockedOutput()  // never follow the output to somewhere else
        audioLock.lock()
        // stop the old player first: that's what makes it hand back (complete)
        // the buffers it still holds, so the queue's slots are freed
        player.stop()
        engine.stop()
        buildEngine()
        audioLock.unlock()
        FileHandle.standardError.write("dromac-audio: output format changed; rebuilt on \(currentOutputName())\n".data(using: .utf8)!)
    }
}
buildEngine()
// Backstop in case a device change arrives without a configuration change.
let watchdog = DispatchSource.makeTimerSource(queue: .global())
watchdog.schedule(deadline: .now() + 0.25, repeating: 0.25)
watchdog.setEventHandler { leaveIfNotLockedOutput() }
watchdog.resume()

// The Mac is about to sleep: stop now, so the server can give the phone its
// audio back while there's still time.
NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: nil) { _ in
    FileHandle.standardError.write("dromac-audio: Mac going to sleep\n".data(using: .utf8)!)
    exit(4)
}

// ---- the headphones' buttons, and the Mac's "Now Playing" ----
// macOS sends play/pause/next/previous from headphones (and the media keys) to
// whichever app is Now Playing; with none, it launches Apple Music. So this
// registers as Now Playing -- showing the phone's song in Control Center --
// and passes the buttons on to Dromac's server, which tells the phone. The
// song info comes from now_playing.json, kept up to date by the server.
let nowPlayingFile = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
    .appendingPathComponent("now_playing.json")
var phoneIsPlaying = true

func tellServer(_ path: String) {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:8811" + path)!)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = "{}".data(using: .utf8)
    URLSession.shared.dataTask(with: request).resume()
}

let commands = MPRemoteCommandCenter.shared()
commands.togglePlayPauseCommand.addTarget { _ in
    phoneIsPlaying.toggle()
    MPNowPlayingInfoCenter.default().playbackState = phoneIsPlaying ? .playing : .paused
    tellServer("/api/media/play_pause")
    return .success
}
// (play and pause are passed on as presses too: with rain on there are more
// states than playing/paused -- music, then rain -- and the server steps them)
commands.playCommand.addTarget { _ in tellServer("/api/media/play_pause"); return .success }
commands.pauseCommand.addTarget { _ in tellServer("/api/media/play_pause"); return .success }
commands.nextTrackCommand.addTarget { _ in tellServer("/api/media/next"); return .success }
commands.previousTrackCommand.addTarget { _ in tellServer("/api/media/prev"); return .success }

var lastArtworkKey = ""
var artwork: MPMediaItemArtwork?
func refreshNowPlaying() {
    guard let data = try? Data(contentsOf: nowPlayingFile),
          let np = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
    phoneIsPlaying = (np["playing"] as? Bool) ?? phoneIsPlaying
    var info: [String: Any] = [
        MPMediaItemPropertyTitle: np["title"] as? String ?? "Phone audio",
        MPMediaItemPropertyArtist: np["artist"] as? String ?? "",
        MPNowPlayingInfoPropertyPlaybackRate: phoneIsPlaying ? 1.0 : 0.0,
    ]
    if let d = np["duration_ms"] as? Double, d > 0 { info[MPMediaItemPropertyPlaybackDuration] = d / 1000 }
    if let pos = np["position_ms"] as? Double { info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = pos / 1000 }
    if let path = np["artwork"] as? String, path != lastArtworkKey {
        lastArtworkKey = path
        if let image = NSImage(contentsOfFile: path) {
            artwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
        } else {
            artwork = nil
        }
    }
    if let a = artwork { info[MPMediaItemPropertyArtwork] = a }
    MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    MPNowPlayingInfoCenter.default().playbackState = phoneIsPlaying ? .playing : .paused
}
refreshNowPlaying()
Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in refreshNowPlaying() }

// How many buffers to keep queued: maxQueued normally; fewer while head
// tracking is on (player_cfg.json, from dromac_fx.py), so the sound turns with
// your head sooner; more under ultramusemax's heavy load. Lowering it never
// drops audio -- the queue just plays down, and the slack moves upstream,
// before the 8D (where it costs nothing).
var queueTarget = maxQueued
var inFlight = 0
let flightLock = NSLock()
let cfgFile = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
    .appendingPathComponent("player_cfg.json")
var lastRcvbuf = 0
Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in
    guard let data = try? Data(contentsOf: cfgFile),
          let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
    if let q = cfg["queue"] as? Int { queueTarget = max(2, min(queueCapacity, q)) }
    if let r = cfg["rcvbuf"] as? Int, r != lastRcvbuf {
        lastRcvbuf = r
        var v = Int32(r)
        setsockopt(0, SOL_SOCKET, SO_RCVBUF, &v, socklen_t(MemoryLayout<Int32>.size))
    }
}
let input = FileHandle.standardInput

Thread {
var pending = Data()
var queued = 0
var started = false

while true {
    let chunk = input.readData(ofLength: bytesPerBuffer)
    if chunk.isEmpty { break }
    pending.append(chunk)
    while pending.count >= bytesPerBuffer {
        let block = pending.prefix(bytesPerBuffer)
        pending.removeFirst(bytesPerBuffer)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(framesPerBuffer)) else { continue }
        buffer.frameLength = AVAudioFrameCount(framesPerBuffer)
        let left = buffer.floatChannelData![0]
        let right = buffer.floatChannelData![1]
        block.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<framesPerBuffer {
                left[i] = Float(Int16(littleEndian: samples[2 * i])) / 32768
                right[i] = Float(Int16(littleEndian: samples[2 * i + 1])) / 32768
            }
        }
        while true {
            flightLock.lock(); let n = inFlight; flightLock.unlock()
            if n < queueTarget { break }
            usleep(1000)
        }
        flightLock.lock(); inFlight += 1; flightLock.unlock()
        audioLock.lock()
        player.scheduleBuffer(buffer) { flightLock.lock(); inFlight -= 1; flightLock.unlock() }
        audioLock.unlock()
        queued += 1
        if !started && queued >= maxQueued {
            started = true
            audioLock.lock()
            playbackStarted = true
            player.play()
            audioLock.unlock()
        }
    }
}
exit(0)  // stdin ended
}.start()

// A real (Dock-less) app event loop: macOS only lets an app become "Now
// Playing" -- and so get the headphones' play/pause instead of Apple Music
// opening -- when it runs as an application, not a bare run loop.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
MPNowPlayingInfoCenter.default().playbackState = phoneIsPlaying ? .playing : .paused
app.run()
