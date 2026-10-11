"""Editable synthetic world. IDs and dates are stable across database resets.

Entity IDs are ground truth for tests; they never appear in scraped page content.
Image fixtures are lookup keys, not image recognition or real photographs.
"""

SEED_VERSION = "1"

PEOPLE = [
    dict(id="alex-dev", name="Alex Morgan", handle="alexbuilds", location="Kansas City, MO",
         workplace="Prairie Atlas Labs", role="Software engineer",
         bio="I build open-source transit maps and volunteer at the Riverbend maker club.",
         topic="A weekend building accessible transit maps", alias="Alex M."),
    dict(id="alex-photo", name="Alex Morgan", handle="alexframes", location="Portland, OR",
         workplace="Cedar Light Studio", role="Landscape photographer",
         bio="Film cameras, coastal trails, and photography workshops in the Pacific Northwest.",
         topic="Photographing the coast in autumn", alias="Alex Morgan Photo"),
    dict(id="sam-design", name="Sam Rivera", handle="samdraws", location="Kansas City, MO",
         workplace="Mosaic Kite Design", role="Product designer",
         bio="Designing accessible civic tools. Sketchbook enthusiast and community mentor.",
         topic="How we tested our library wayfinding prototype", alias="Sammy Rivera"),
    dict(id="jordan-science", name="Jordan Lee", handle="jordanwater", location="Madison, WI",
         workplace="Northlake Research Cooperative", role="Freshwater researcher",
         bio="Studying lake water quality and sharing open environmental datasets.",
         topic="A field notebook from the Northlake survey", alias="J. Lee"),
    dict(id="maya-garden", name="Maya Chen", handle="mayagrows", location="Omaha, NE",
         workplace="Sunseed Community Gardens", role="Garden coordinator",
         bio="Neighborhood gardens, native plants, and practical compost experiments.",
         topic="Our neighborhood seed swap is back", alias="Maya C."),
    dict(id="casey-music", name="Casey Brooks", handle="caseykeys", location="Tulsa, OK",
         workplace="Blue Finch Music School", role="Piano teacher",
         bio="Piano lessons, small jazz ensembles, and beginner music theory.",
         topic="Making room for a first recital", alias="C. Brooks"),
]

PLATFORMS = {
    "professional": ("linkup.test", "in/"),
    "social": ("chatter.test", ""),
    "photos": ("photogram.test", ""),
    "code": ("codeforge.test", ""),
    "blog": ("fieldnotes.test", "@"),
}


def build_world():
    pages, links = [], []

    def add(page_id, url, title, body, *, entity=None, platform="web", kind="article",
            name="", bio="", workplace="", location="", published="2026-10-01",
            status=200, canonical=None):
        pages.append(dict(id=page_id, url=url, title=title, body=body, entity_id=entity,
                          platform=platform, kind=kind, display_name=name, bio=bio,
                          workplace=workplace, location=location, published_at=published,
                          status_code=status, canonical_id=canonical))
        return page_id

    for person in PEOPLE:
        person_id = person["id"]
        for platform, (host, prefix) in PLATFORMS.items():
            # One person has a genuinely sparse public footprint.
            if person_id == "casey-music" and platform not in ("professional", "social"):
                continue
            name = person["alias"] if platform in ("social", "photos") else person["name"]
            # A stale employer is intentional evidence for freshness tests.
            employer = "Old Mill Software" if person_id == "alex-dev" and platform == "photos" else person["workplace"]
            date = "2022-05-14" if employer == "Old Mill Software" else "2026-10-01"
            add(f"{person_id}-{platform}", f"https://{host}/{prefix}{person['handle']}",
                f"{name} | {host.split('.')[0].title()}",
                f"{person['role']} at {employer}. {person['bio']}",
                entity=person_id, platform=platform, kind="profile", name=name,
                bio=person["bio"], workplace=employer, location=person["location"], published=date)

        professional, social = f"{person_id}-professional", f"{person_id}-social"
        links.extend([(professional, social, "Social profile"), (social, professional, "Work profile")])
        if person_id != "casey-music":
            code, photos, blog = (f"{person_id}-{p}" for p in ("code", "photos", "blog"))
            links.extend([(professional, code, "Projects"), (photos, social, "Updates"),
                          (photos, blog, "Read my blog"), (blog, professional, "About the author"),
                          (blog, code, "Source code"), (social, blog, "My writing")])

        for index in range(1, 3):
            post_id = add(f"{person_id}-post-{index}",
                          f"https://chatter.test/{person['handle']}/status/{100 + index}",
                          f"{person['alias']}: {person['topic']}",
                          (f"{person['topic']}. Sharing notes from {person['location']}. "
                           f"{person['bio']} #community" if index == 1 else
                           f"Thanks to everyone at {person['workplace']} for today's workshop. "
                           "The notes are on my profile. Looking forward to the next meetup!"),
                          entity=person_id, platform="social", kind="post", name=person["alias"],
                          published=f"2026-10-0{index + 2}")
            links.extend([(post_id, social, "Author"), (social, post_id, f"Recent post {index}")])

    add("alex-interview", "https://riverbend-news.test/people/alex-morgan-transit",
        "Alex Morgan helps neighbors navigate Riverbend transit",
        "Alex Morgan, a software engineer at Prairie Atlas Labs in Kansas City, "
        "built an accessible transit map with the Riverbend maker club. "
        "Designer Sam Rivera of Mosaic Kite Design helped test the signs.", entity="alex-dev")
    links.extend([("alex-interview", "alex-dev-professional", "Alex Morgan"),
                  ("alex-interview", "sam-design-professional", "Sam Rivera")])
    add("alex-old-bio", "https://archive.test/speakers/alex-morgan-2021",
        "Alex Morgan — 2021 Riverbend speaker",
        "Alex Morgan is a junior developer at Old Mill Software in St. Louis. "
        "This speaker biography was last updated in 2021.", entity="alex-dev",
        name="Alex Morgan", workplace="Old Mill Software", location="St. Louis, MO", published="2021-06-12")
    add("alex-mirror", "https://syndicated.test/transit/alex-morgan",
        "Alex Morgan helps neighbors navigate Riverbend transit",
        next(p["body"] for p in pages if p["id"] == "alex-interview"),
        entity="alex-dev", canonical="alex-interview")
    links.append(("alex-mirror", "alex-interview", "Original article"))
    add("alex-deleted", "https://fieldnotes.test/@alexbuilds/old-project",
        "Alex Morgan: an old transit project", "This post was removed by its author.",
        entity="alex-dev", status=404)
    add("sam-private", "https://photogram.test/samdraws/private-album",
        "Sam Rivera: workshop photos", "This album requires signing in.",
        entity="sam-design", platform="photos", status=403)
    links.extend([("alex-dev-blog", "alex-deleted", "My old project"),
                  ("sam-design-photos", "sam-private", "Workshop album")])
    add("makers-event", "https://riverbend-makers.test/events/october",
        "Riverbend maker club: October open house",
        "Meet Alex Morgan and Sam Rivera at our October open house in Kansas City. "
        "Bring a transit map idea, a sketchbook, or a half-finished community project.")
    links.extend([("makers-event", "alex-dev-blog", "Alex's notes"),
                  ("makers-event", "sam-design-blog", "Sam's notes")])
    add("noise-coffee", "https://everyday.test/coffee", "A guide to making cold brew",
        "Coffee, a clean jar, cool water, and an overnight wait are all you need.")
    add("noise-transit", "https://everyday.test/transit", "Transit maps through the years",
        "A short history of transit map typography and accessible wayfinding.")

    fixtures = {
        "alex-front": ["alex-dev-professional", "alex-dev-social", "alex-interview", "alex-dev-code"],
        "alex-left": ["alex-dev-photos", "alex-dev-blog", "alex-dev-social", "alex-mirror"],
        "alex-right": ["alex-interview", "alex-dev-code", "alex-dev-post-1", "alex-old-bio", "alex-deleted"],
        "sam-front": ["sam-design-professional", "sam-design-social", "sam-design-photos", "sam-private"],
        "casey-front": ["casey-music-professional", "casey-music-social"],
        "unknown": [],
    }
    return pages, links, fixtures

