// Initial layout for new Sentinel users; this is not reapplied on every login.
var panel = new Panel;
panel.location = "top";
panel.height = 44;
panel.lengthMode = "fill";
panel.alignment = "center";
panel.hiding = "none";
panel.floating = true;
panel.opacity = "opaque";
var launcher = panel.addWidget("org.kde.plasma.kickoff");
launcher.currentConfigGroup = ["General"];
launcher.writeConfig("icon", "/usr/share/sentinel/assets/Sentinel_Shield_Minimal_Solid-Visor.svg");
// Kickoff imports the editable kicker-extra-favoritesrc defaults on first use.
var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
tasks.writeConfig("launchers", ["applications:org.kde.konsole.desktop", "applications:sentinel-appstore.desktop",
    "applications:org.kde.dolphin.desktop", "applications:systemsettings.desktop",
    "applications:sentinel-portal.desktop", "applications:google-chrome.desktop"]);
panel.addWidget("org.kde.plasma.panelspacer");
panel.addWidget("org.kde.plasma.systemtray");
panel.addWidget("org.kde.plasma.digitalclock");
panel.addWidget("org.kde.plasma.showdesktop");
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; ++i) {
    allDesktops[i].wallpaperPlugin = "org.kde.image";
    allDesktops[i].currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    allDesktops[i].writeConfig("Image", "file:///usr/share/wallpapers/SentinelGradient");
    allDesktops[i].writeConfig("FillMode", 2);
}
