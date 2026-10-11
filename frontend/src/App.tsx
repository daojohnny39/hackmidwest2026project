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
      ['03', 'Review', 'Explore sample results. Live search is not connected.', 'globe'],
    ].map(([number, title, body, icon]) => <article className="process-card" key={number}><div className="process-top"><span>{number}</span><Icon name={icon} /></div><h2>{title}</h2><p>{body}</p></article>)}</div>
    <section className="principles"><div className="principles-art"><Orbit /></div><div><h2>Your privacy</h2>
      <details open><summary>Photos stay in this tab<Icon name="plus" /></summary><p>No uploads in this demo. Clear, reload, or leave Home to discard photos.</p></details>
      <details><summary>Public sources only<Icon name="plus" /></summary><p>Planned searches require consent and exclude private accounts.</p></details>
      <details><summary>Confidence needs review<Icon name="plus" /></summary><p>Confidence reflects contextual evidence and does not verify identity. Check each original source.</p></details>
      <details><summary>Demo status<Icon name="plus" /></summary><p>Face detection runs locally on each photo. Public-source search is not connected. Storage terms must be confirmed before uploads are enabled.</p></details>
    </div></section>
    <div className="page-cta"><a className="button primary" href="#studio">Open Home <Icon name="arrow" /></a></div>
  </div>
}
function Team() {
  return <div className="subpage team-page"><div className="page-intro"><h1>Our team</h1><p>Five developers. Hack Midwest 2026.</p></div>
    <div className="team-grid">{team.map((member, index) => <article className="team-card" key={member.id}><div className={`team-portrait portrait-${index}`} aria-hidden="true"><Mark /><span>0{index + 1}</span><div className="portrait-orbit" /></div><div className="team-copy"><h2>{member.name}</h2><p>{member.role}</p></div></article>)}</div>
  </div>
}
