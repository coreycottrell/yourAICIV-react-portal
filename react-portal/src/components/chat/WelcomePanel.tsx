import { useIdentityStore } from '../../stores/identityStore'
import { useTrialStore } from '../../stores/trialStore'
import { BrandGlyph } from '../brand/BrandMark'
import { Icon } from '../common/Icon'
import './WelcomePanel.css'

interface Starter {
  title: string
  desc: string
  prompt: string
}

const STARTERS: Starter[] = [
  {
    title: 'Get to know my business',
    desc: 'Start here. Your AI asks the questions it needs.',
    prompt:
      "Let's start with my business. Ask me what you need to know about my goals, my customers and what I sell, one question at a time.",
  },
  {
    title: 'Build my website',
    desc: 'A real page for your business, with a way to reach you.',
    prompt: 'Build me a simple website for my business. Ask me what you need first.',
  },
  {
    title: 'Plan my week',
    desc: 'What matters most in the next seven days.',
    prompt: "Help me plan this week. What should I focus on, and what can you take off my plate?",
  },
  {
    title: 'Show me what you can do',
    desc: 'Three useful things it can do for you right now.',
    prompt: 'What are the three most useful things you can do for my business this week? Then start on the first one.',
  },
]

/** First-run screen shown when the chat is empty. */
export function WelcomePanel({ onPick }: { onPick: (prompt: string) => void }) {
  const civName = useIdentityStore(s => s.civName)
  const humanName = useIdentityStore(s => s.humanName)
  const trial = useTrialStore(s => s.status)

  const hello = humanName ? `Hi ${humanName.split(' ')[0]}` : 'Hi there'
  const aiName = civName && civName !== 'AiCIV' ? civName : 'your AI'

  return (
    <section className="welcome" aria-labelledby="welcome-title">
      <BrandGlyph size={48} />
      <h2 id="welcome-title" className="welcome-title">
        {hello}, I&rsquo;m {aiName}.
      </h2>
      <p className="welcome-sub">
        I work for your business around the clock. Tell me what you&rsquo;re working toward and
        I&rsquo;ll start building. Pick one to begin, or just type below.
      </p>
      {trial.trial && !trial.expired && (
        <p className="welcome-trial">
          Day {trial.day} of {trial.duration_days || 7} of your free trial. Everything I build stays yours.
        </p>
      )}
      <div className="welcome-grid">
        {STARTERS.map(s => (
          <button key={s.title} type="button" className="welcome-card" onClick={() => onPick(s.prompt)}>
            <span className="welcome-card-title">
              {s.title}
              <Icon name="arrow" size={16} className="welcome-card-arrow" />
            </span>
            <span className="welcome-card-desc">{s.desc}</span>
          </button>
        ))}
      </div>
    </section>
  )
}
