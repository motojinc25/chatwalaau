import { useCallback, useRef, useState } from 'react'

export type VoiceState = 'idle' | 'recording' | 'transcribing'

interface UseVoiceInputReturn {
  voiceState: VoiceState
  waveformData: number[]
  startRecording: () => Promise<void>
  stopRecording: () => void
  error: string | null
}

const ANALYSER_FFT_SIZE = 64
const WAVEFORM_BARS = ANALYSER_FFT_SIZE / 2

export function useVoiceInput(onTranscribed: (text: string) => void): UseVoiceInputReturn {
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [waveformData, setWaveformData] = useState<number[]>(() => Array.from({ length: WAVEFORM_BARS }, () => 0))
  const [error, setError] = useState<string | null>(null)

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const animationRef = useRef<number | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  const cleanup = useCallback(() => {
    if (animationRef.current !== null) {
      cancelAnimationFrame(animationRef.current)
      animationRef.current = null
    }
    if (audioContextRef.current) {
      audioContextRef.current.close()
      audioContextRef.current = null
    }
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) {
        track.stop()
      }
      streamRef.current = null
    }
    analyserRef.current = null
    mediaRecorderRef.current = null
    chunksRef.current = []
    setWaveformData(Array.from({ length: WAVEFORM_BARS }, () => 0))
  }, [])

  const updateWaveform = useCallback(() => {
    const analyser = analyserRef.current
    if (!analyser) return

    const dataArray = new Uint8Array(analyser.frequencyBinCount)
    analyser.getByteFrequencyData(dataArray)

    const normalized = Array.from(dataArray, (v) => v / 255)
    setWaveformData(normalized)

    animationRef.current = requestAnimationFrame(updateWaveform)
  }, [])

  const startRecording = useCallback(async () => {
    setError(null)

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

      const audioContext = new AudioContext()
      audioContextRef.current = audioContext
      const source = audioContext.createMediaStreamSource(stream)
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = ANALYSER_FFT_SIZE
      source.connect(analyser)
      analyserRef.current = analyser

      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : ''

      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      mediaRecorderRef.current = recorder
      chunksRef.current = []

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          chunksRef.current.push(e.data)
        }
      }

      recorder.onstop = async () => {
        const chunks = chunksRef.current
        if (chunks.length === 0) {
          cleanup()
          setVoiceState('idle')
          return
        }

        const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' })
        cleanup()
        setVoiceState('transcribing')

        try {
          const formData = new FormData()
          formData.append('file', blob, 'recording.webm')

          const res = await fetch('/api/transcribe', {
            method: 'POST',
            body: formData,
          })

          if (!res.ok) {
            const detail = await res.text()
            throw new Error(`Transcription failed: ${detail}`)
          }

          const data = await res.json()
          if (data.text) {
            onTranscribed(data.text)
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Transcription failed')
        } finally {
          setVoiceState('idle')
        }
      }

      recorder.start(250)
      setVoiceState('recording')
      updateWaveform()
    } catch (err) {
      cleanup()
      setVoiceState('idle')
      if (err instanceof DOMException && err.name === 'NotAllowedError') {
        setError('Microphone access denied')
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start recording')
      }
    }
  }, [onTranscribed, cleanup, updateWaveform])

  const stopRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current
    if (recorder && recorder.state === 'recording') {
      recorder.stop()
    }
  }, [])

  return {
    voiceState,
    waveformData,
    startRecording,
    stopRecording,
    error,
  }
}
