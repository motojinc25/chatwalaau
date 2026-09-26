import { useEffect, useState } from 'react'

/**
 * Microphone level bars for a live MediaStream (CTR-0020 / CTR-0228, PRP-0188).
 *
 * The analyser loop Voice Input used to own, shared now with the Live conversation so
 * both draw the same meter: AudioContext -> AnalyserNode (fftSize 64, so 32 bars) ->
 * getByteFrequencyData on every animation frame, normalised to 0..1. A null stream
 * yields flat bars; changing or clearing the stream tears the graph down.
 */
const ANALYSER_FFT_SIZE = 64
export const WAVEFORM_BARS = ANALYSER_FFT_SIZE / 2

const flat = () => Array.from({ length: WAVEFORM_BARS }, () => 0)

export function useMicLevel(stream: MediaStream | null): number[] {
  const [levels, setLevels] = useState<number[]>(flat)

  useEffect(() => {
    if (!stream) {
      setLevels(flat())
      return
    }
    const audioContext = new AudioContext()
    const source = audioContext.createMediaStreamSource(stream)
    const analyser = audioContext.createAnalyser()
    analyser.fftSize = ANALYSER_FFT_SIZE
    source.connect(analyser)
    const data = new Uint8Array(analyser.frequencyBinCount)
    let frame = 0
    const tick = () => {
      analyser.getByteFrequencyData(data)
      setLevels(Array.from(data, (v) => v / 255))
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(frame)
      source.disconnect()
      void audioContext.close()
      setLevels(flat())
    }
  }, [stream])

  return levels
}
