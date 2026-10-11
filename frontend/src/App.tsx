import { useEffect, useState } from 'react'
import { Studio } from './components/Studio'
import { Icon, Mark, Orbit } from './components/Artwork'
import { team } from './data/team'

type Page = 'studio' | 'about' | 'team'
function currentPage(): Page {
  const hash = window.location.hash.slice(1)
  return hash === 'about' || hash === 'team' ? hash : 'studio'
}
export default function App() {
  const [page, setPage] = useState<Page>(currentPage)
  useEffect(() => {
    const navigate = () => { setPage(currentPage()); window.scrollTo({ top: 0 }) }
    window.addEventListener('hashchange', navigate)
    return () => window.removeEventListener('hashchange', navigate)
  }, [])
  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus(); document.getElementById('main-content')?.scrollIntoView() }}>Skip to content</a>
    <header className="site-header">
      <a className="wordmark" href="#studio" aria-label="CyberPI home"><Mark />Cyber<span className="wordmark-pi">PI</span><span className="wordmark-dot">.</span></a>
      <nav aria-label="Main navigation">{([['studio', 'Home'], ['about', 'About'], ['team', 'Team']] as const).map(([id, title]) => <a key={id} href={`#${id}`} aria-current={page === id ? 'page' : undefined}>{title}</a>)}</nav>
      <span className="edition"><span className="status-dot" /> Hack Midwest ’26</span>
    </header>
    <main id="main-content" tabIndex={-1}>{page === 'studio' ? <Studio /> : page === 'about' ? <About /> : <Team />}</main>
    <footer className="site-footer"><span><Mark /> CyberPI</span><a href="?capture=automatic#studio">Automatic capture <Icon name="arrow" /></a><a href="#about">About <Icon name="arrow" /></a></footer>
  </div>
}
function About() {
  return <div className="subpage">
    <div className="page-intro"><h1>How it works</h1><p>No account needed.</p></div>
    <div className="process-grid">{[
      ['01', 'Consent', 'Agree before opening your camera.', 'shield'],
      ['02', 'Capture', 'Capture one reference photo automatically, or choose manual capture.', 'camera'],
      ['03', 'Search', 'Find public pages that use your face, each with a confidence level.', 'globe'],
    ].map(([number, title, body, icon]) => <article className="process-card" key={number}><div className="process-top"><span>{number}</span><Icon name={icon} /></div><h2>{title}</h2><p>{body}</p></article>)}</div>
    <section className="principles"><div className="principles-art"><Orbit /></div><div><h2>Your privacy</h2>
      <details open><summary>Photos stay in this tab until you search<Icon name="plus" /></summary><p>Nothing is uploaded until you search. Clear, reload, or leave Home to discard photos in the tab.</p></details>
      <details><summary>Public sources only<Icon name="plus" /></summary><p>Planned searches require consent and exclude private accounts.</p></details>
      <details><summary>Confidence needs review<Icon name="plus" /></summary><p>Confidence is how closely a face matches your photo and does not verify identity. Check each original source.</p></details>
      <details><summary>Demo status<Icon name="plus" /></summary><p>Face detection runs locally on each photo. A search sends a crop of your face to Trace, Yandex and Bing (through SerpApi), and Amazon Rekognition. The demo server keeps the photo and results on its disk. Source risk checks are not connected yet.</p></details>
    </div></section>
    <div className="page-cta"><a className="button primary" href="#studio">Open Home <Icon name="arrow" /></a></div>
  </div>
}
function Team() {
  return <div className="subpage team-page"><div className="page-intro"><h1>Our team</h1></div>
    <div className="team-grid">{team.map(member => <article className="team-card" key={member.id}><div className="team-portrait"><img src={`${import.meta.env.BASE_URL}${member.photo}`} alt={member.name} loading="lazy" decoding="async" style={{ objectPosition: member.photoPosition }} /></div><div className="team-copy"><h2>{member.name}</h2><p>{member.role}</p></div></article>)}</div>
  </div>
}
