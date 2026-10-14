CATEGORY_MAP = {
    "WebBrowser": ["Browsers"],
    "Network": ["Communication"],
    "InstantMessaging": ["Communication"],
    "Chat": ["Communication"],
    "AudioVideo": ["Media"],
    "Music": ["Music"],
    "Development": ["Development"],
    "IDE": ["Development", "Coding"],
    "Game": ["Gaming"],
    "Education": ["Education"],
    "Science": ["Education"],
    "Math": ["Education"],
    "Utility": ["Utilities"],
    "Emulator": ["Emulation"],
    "TerminalEmulator": ["Coding"],
}


def normalize_categories(
    categories: list[str],
) -> list[str]:

    normalized = []

    for category in categories:
        mapped_categories = CATEGORY_MAP.get(
            category,
            [],
        )

        for mapped in mapped_categories:
            if mapped not in normalized:
                normalized.append(mapped)

    return normalized
