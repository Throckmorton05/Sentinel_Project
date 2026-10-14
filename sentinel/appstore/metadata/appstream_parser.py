from pathlib import Path
import xml.etree.ElementTree as ET


APPSTREAM_PATH = Path("/var/lib/flatpak/appstream/flathub/x86_64/active/appstream.xml")


ICONS_64_PATH = Path("/var/lib/flatpak/appstream/flathub/x86_64/active/icons/64x64")


def parse_appstream() -> list[dict[str, object]]:

    tree = ET.parse(APPSTREAM_PATH)

    root = tree.getroot()

    apps = []

    for component in root.findall("component"):
        app_id = component.findtext("id")

        name = component.findtext("name")

        summary = component.findtext("summary")

        if not app_id or not name:
            continue

        categories = []

        categories_element = component.find("categories")

        if categories_element is not None:
            for category in categories_element.findall("category"):
                if category.text:
                    categories.append(category.text)

        icon_name = None
        icon_path = None

        icon_element = component.find("icon")

        if icon_element is not None and icon_element.text:
            icon_name = icon_element.text

            possible_icon_path = ICONS_64_PATH / icon_name

            if possible_icon_path.exists():
                icon_path = str(possible_icon_path)

        apps.append(
            {
                "flatpak_id": app_id,
                "name": name,
                "summary": summary or "",
                "categories": categories,
                "icon_name": icon_name,
                "icon_path": icon_path,
            }
        )

    return apps


if __name__ == "__main__":
    apps = parse_appstream()

    print(f"Loaded {len(apps)} apps")

    for app in apps[:10]:
        print()
        print(app["name"])
        print(app["flatpak_id"])
        print(app["categories"])
        print(app["icon_name"])
        print(app["icon_path"])
