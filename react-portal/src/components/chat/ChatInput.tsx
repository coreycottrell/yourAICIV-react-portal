import { useState, useRef, useEffect, type FormEvent, type KeyboardEvent } from 'react'
import { useSettingsStore } from '../../stores/settingsStore'
import { useSpeechRecognition } from '../../hooks/useSpeechRecognition'
import { apiGet } from '../../api/client'
import { Icon } from '../common/Icon'
import './ChatInput.css'

interface SlashCommand {
  cmd: string
  desc: string
  type: string
}

interface ChatInputProps {
  onSend: (text: string) => void
  onUpload: (file: File) => void
  sending: boolean
}

export function ChatInput({ onSend, onUpload, sending }: ChatInputProps) {
  const [text, setText] = useState('')
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([])
  const [showSlash, setShowSlash] = useState(false)
  const [slashIndex, setSlashIndex] = useState(0)
  const fileRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const quickfirePills = useSettingsStore(s => s.quickfirePills)
  const { isListening, isSupported, transcript, start, stop } = useSpeechRecognition()

  // Fetch slash commands once
  useEffect(() => {
    apiGet<{ slash_commands: SlashCommand[] }>('/api/shortcuts')
      .then(data => setSlashCommands(data.slash_commands || []))
      .catch(() => {})
  }, [])

  // Sync speech transcript into text
  useEffect(() => {
    if (isListening && transcript) {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mirror the external speech-recognition transcript
      setText(transcript)
    }
  }, [transcript, isListening])

  const filteredCommands = text.startsWith('/')
    ? slashCommands.filter(c => c.cmd.toLowerCase().startsWith(text.toLowerCase()))
    : []

  useEffect(() => {
  // eslint-disable-next-line react-hooks/set-state-in-effect -- reopen/reset the slash menu when the typed text changes
    setShowSlash(text.startsWith('/') && filteredCommands.length > 0)
    setSlashIndex(0)
  }, [text, filteredCommands.length])

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = text.trim()
    if (!trimmed || sending) return
    onSend(trimmed)
    setText('')
    setShowSlash(false)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const selectSlashCommand = (cmd: string) => {
    setText(cmd + ' ')
    setShowSlash(false)
    textareaRef.current?.focus()
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlash) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSlashIndex(i => Math.min(i + 1, filteredCommands.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSlashIndex(i => Math.max(i - 1, 0))
        return
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        if (filteredCommands[slashIndex]) {
          selectSlashCommand(filteredCommands[slashIndex].cmd)
        }
        return
      }
      if (e.key === 'Escape') {
        setShowSlash(false)
        return
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 150) + 'px'
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      onUpload(file)
      e.target.value = ''
    }
  }

  const toggleMic = () => {
    if (isListening) {
      stop()
    } else {
      start()
    }
  }

  return (
    <div className="chat-input-container">
      {quickfirePills.length > 0 && (
        <div className="chat-pills">
          {quickfirePills.map((pill) => (
            <button
              key={pill}
              className="chat-pill"
              onClick={() => onSend(pill)}
              disabled={sending}
            >
              {pill}
            </button>
          ))}
        </div>
      )}
      <form className="chat-input-form" onSubmit={handleSubmit}>
        <button
          type="button"
          className="chat-upload-btn"
          onClick={() => fileRef.current?.click()}
          title="Upload file"
        >
          +
        </button>
        <input
          type="file"
          ref={fileRef}
          className="sr-only"
          onChange={handleFileChange}
        />
        <div className="chat-textarea-wrap">
          {showSlash && (
            <div className="slash-dropdown">
              {filteredCommands.map((cmd, i) => (
                <button
                  key={cmd.cmd}
                  type="button"
                  className={`slash-item ${i === slashIndex ? 'slash-item-active' : ''}`}
                  onClick={() => selectSlashCommand(cmd.cmd)}
                  onMouseEnter={() => setSlashIndex(i)}
                >
                  <span className="slash-cmd">{cmd.cmd}</span>
                  <span className="slash-desc">{cmd.desc}</span>
                </button>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder="Type a message... (/ for commands)"
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            rows={1}
            disabled={sending}
          />
        </div>
        {isSupported && (
          <button
            type="button"
            className={`chat-mic-btn ${isListening ? 'chat-mic-active' : ''}`}
            onClick={toggleMic}
            title={isListening ? 'Stop recording' : 'Voice input'}
            aria-label={isListening ? 'Stop recording' : 'Voice input'}
          >
            <Icon name="mic" size={18} />
          </button>
        )}
        <button
          type="submit"
          className="chat-send-btn"
          disabled={!text.trim() || sending}
          aria-label="Send message"
        >
          {sending ? '...' : <Icon name="send" size={18} />}
        </button>
      </form>
    </div>
  )
}
