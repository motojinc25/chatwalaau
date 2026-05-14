import { useCallback, useRef, useState } from 'react'
import { extractPlainText } from '@/lib/extractPlainText'

export type TTSState = 'idle' | 'loading' | 'playing'
export type DownloadState = 'idle' | 'downloading'

interface UseTTSReturn {
  ttsState: TTSState
  downloadState: DownloadState
  playingMessageId: string | null
  downloadingMessageId: string | null
  play: (text: string, messageId: string) => Promise<void>
  stop: () => void
  download: (text: string, messageId: string, filename: string) => Promise<void>
  error: string | null
}

async function fetchTTSAudio(text: string): Promise<Blob> {
  const plainText = extractPlainText(text)
  const res = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: plainText }),
  })
  if (!res.ok) {
    const detail = await res.text()
    throw new Error(res.status === 503 ? 'TTS is not configured' : `Speech synthesis failed: ${detail}`)
  }
  return res.blob()
}

export function useTTS(): UseTTSReturn {
  const [ttsState, setTTSState] = useState<TTSState>('idle')
  const [downloadState, setDownloadState] = useState<DownloadState>('idle')
  const [playingMessageId, setPlayingMessageId] = useState<string | null>(null)
  const [downloadingMessageId, setDownloadingMessageId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const cacheRef = useRef<Map<string, string>>(new Map())

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current.currentTime = 0
      audioRef.current = null
    }
    setTTSState('idle')
    setPlayingMessageId(null)
  }, [])

  const play = useCallback(
    async (text: string, messageId: string) => {
      setError(null)

      // If already playing this message, stop it
      if (audioRef.current && playingMessageId === messageId) {
        stop()
        return
      }

      // Stop any currently playing audio
      stop()

      setTTSState('loading')
      setPlayingMessageId(messageId)

      try {
        let blobUrl = cacheRef.current.get(messageId)
        if (!blobUrl) {
          const blob = await fetchTTSAudio(text)
          blobUrl = URL.createObjectURL(blob)
          cacheRef.current.set(messageId, blobUrl)
        }

        const audio = new Audio(blobUrl)
        audioRef.current = audio

        audio.onended = () => {
          audioRef.current = null
          setTTSState('idle')
          setPlayingMessageId(null)
        }

        audio.onerror = () => {
          audioRef.current = null
          setTTSState('idle')
          setPlayingMessageId(null)
          setError('Audio playback failed')
        }

        await audio.play()
        setTTSState('playing')
      } catch (err) {
        setTTSState('idle')
        setPlayingMessageId(null)
        setError(err instanceof Error ? err.message : 'TTS failed')
      }
    },
    [playingMessageId, stop],
  )

  const download = useCallback(async (text: string, messageId: string, filename: string) => {
    setError(null)
    setDownloadState('downloading')
    setDownloadingMessageId(messageId)

    try {
      let blobUrl = cacheRef.current.get(messageId)
      if (!blobUrl) {
        const blob = await fetchTTSAudio(text)
        blobUrl = URL.createObjectURL(blob)
        cacheRef.current.set(messageId, blobUrl)
      }

      const a = document.createElement('a')
      a.href = blobUrl
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setDownloadState('idle')
      setDownloadingMessageId(null)
    }
  }, [])

  return {
    ttsState,
    downloadState,
    playingMessageId,
    downloadingMessageId,
    play,
    stop,
    download,
    error,
  }
}
