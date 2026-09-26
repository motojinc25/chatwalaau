import { useCallback, useRef, useState } from 'react'
import { useMicLevel } from '@/hooks/useMicLevel'

export type VoiceState = 'idle' | 'recording' | 'transcribing'

interface UseVoiceInputReturn {
  voiceState: VoiceState
  waveformData: number[]
  startRecording: () => Promise<void>
  stopRecording: () => void
  error: string | null
}

export function useVoiceInput(onTranscribed: (text: string) => void): UseVoiceInputReturn {
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [error, setError] = useState<string | null>(null)
  // The level meter follows the recording stream (the shared analyser, PRP-0188).
  const [stream, setStream] = useState<MediaStream | null>(null)
  const waveformData = useMicLevel(stream)

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  const cleanup = useCallback(() => {
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) {
        track.stop()
      }
      streamRef.current = null
    }
    setStream(null)
    mediaRecorderRef.current = null
    chunksRef.current = []
  }, [])

  const startRecording = useCallback(async () => {
    setError(null)

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

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
      setStream(stream)
      setVoiceState('recording')
    } catch (err) {
      cleanup()
      setVoiceState('idle')
      if (err instanceof DOMException && err.name === 'NotAllowedError') {
        setError('Microphone access denied')
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start recording')
      }
    }
  }, [onTranscribed, cleanup])

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
