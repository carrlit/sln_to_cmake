import xml.etree.ElementTree as ET
from pathlib import Path
import os
import re
import argparse

NS = {"ns": "http://schemas.microsoft.com/developer/msbuild/2003"}

WIN_LIBS = {
    "advapi32", "comdlg32", "gdi32", "kernel32", "odbc32", "odbccp32",
    "ole32", "oleaut32", "shell32", "user32", "uuid", "winspool"
}

QT_REGEX = re.compile(r"Qt\d+(Core|Sql|Xml|Gui|Widgets|Network|Concurrent|PrintSupport|Qml|Quick|QmlModels|Test)(d)?$", re.IGNORECASE)

QT_MODULES_MAP = {
    "core": "Core", "gui": "Gui", "widgets": "Widgets", "xml": "Xml",
    "charts": "Charts", "printsupport": "PrintSupport", "sql": "Sql",
    "network": "Network", "qml": "Qml", "quick": "Quick", "concurrent": "Concurrent",
    "qmlmodels": "QmlModels", "test": "Test"
}

MSVC_VAR_RE = re.compile(r"\$\(([^)]+)\)")

class Project:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.type = "exe"
        self.sources = []
        self.headers = []
        self.ui_files = []
        self.resources = []
        self.qm_files = []
        self.images = []
        self.includes = set()
        self.defines = set()
        self.libs = set()
        self.deps = set()
        self.cflags = set()
        self.filters = {}

def quote_if_needed(path: str) -> str:
    if any(c.isspace() for c in path):
        return f'"{path}"'
    return path

def safe_rel(p, base: Path):
    p_str = str(p).replace("\\", "/")

    # если есть CMake переменные — не трогаем
    if "${" in p_str:
        return p_str

    # если уже относительный путь — не трогаем
    if p_str.startswith("../") or p_str.startswith("./"):
        return p_str

    path_obj = Path(p_str)

    # если не абсолютный — тоже оставляем
    if not path_obj.is_absolute():
        return p_str

    try:
        return path_obj.relative_to(base).as_posix()
    except ValueError:
        return os.path.relpath(path_obj, base).replace("\\", "/")

def format_include(inc: str, proj_dir: Path) -> str:
    inc_str = str(inc).replace("\\", "/")

    # заменяем MSVC-переменные $(VAR) -> ${VAR}, но только если есть $
    inc_str = re.sub(r"\$\(([^)]+)\)", r"${\1}", inc_str)

    # если после этого начинается с ${…}, оставляем как есть
    if inc_str.startswith("${"):
        return inc_str

    # иначе относительный путь
    return safe_rel(Path(inc_str), proj_dir)

def resolve_vars(p, sln_dir, proj_dir):
    if not p:
        return None
    # Заменяем только SolutionDir и ProjectDir
    p = p.replace("$(SolutionDir)", str(sln_dir.as_posix()) + "/")
    p = p.replace("$(ProjectDir)", str(proj_dir.as_posix()) + "/")
    # Остальные переменные $(...) остаются
    return p.strip()

def parse_filters(proj: Project):
    filters_file = proj.path.with_suffix(".vcxproj.filters")
    if not filters_file.exists():
        return
    tree = ET.parse(filters_file)
    root = tree.getroot()
    for tag in ("ClCompile", "ClInclude"):
        for node in root.findall(f".//ns:{tag}", NS):
            f = node.get("Include")
            flt = node.find("ns:Filter", NS)
            if f and flt is not None:
                proj.filters[f] = flt.text

def parse_vcxproj(path: Path, sln_dir: Path):
    proj = Project(path)
    proj.name = get_project_name(path)
    proj_dir = path.parent
    tree = ET.parse(path)
    root = tree.getroot()

    # ClCompile (cpp/c)
    for n in root.findall(".//ns:ClCompile", NS):
        f = n.get("Include")
        if f:
            proj.sources.append((proj_dir / f).resolve())

    # ClInclude (h/hpp)
    for n in root.findall(".//ns:ClInclude", NS):
        f = n.get("Include")
        if f:
            proj.headers.append((proj_dir / f).resolve())

    # QtMoc
    for n in root.findall(".//ns:QtMoc", NS):
        f = n.get("HeaderFile")
        if f:
            proj.headers.append((proj_dir / f).resolve())
            
    # QtUic (.ui)
    for n in root.findall(".//ns:QtUic", NS):
        f = n.get("Include")
        if f:
            proj.ui_files.append((proj_dir / f).resolve())

    # QtRcc (qrc) и ResourceCompile (.rc)
    for n in root.findall(".//ns:ResourceCompile", NS):
        f = n.get("Include")
        if f and f.endswith(".qrc"):
            proj.resources.append((proj_dir / f).resolve())
        elif f and f.endswith(".rc"):
            proj.resources.append((proj_dir / f).resolve())

    # <Image Include="...">
    for n in root.findall(".//ns:Image", NS):
        f = n.get("Include")
        if f:
            proj.images.append((proj_dir / f).resolve())

    # AdditionalIncludeDirectories
    for n in root.findall(".//ns:AdditionalIncludeDirectories", NS):
        if not n.text:
            continue
        for d in n.text.split(";"):
            d = d.strip()

            if not d:
                continue

            if d.startswith("%(") and d.endswith(")"):
                continue

            rp = resolve_vars(d, sln_dir, proj_dir)
            if rp:
                proj.includes.add(rp)

    # PreprocessorDefinitions
    for n in root.findall(".//ns:PreprocessorDefinitions", NS):
        if not n.text:
            continue
        for d in n.text.split(";"):
            if "%" in d or not d:
                continue
            proj.defines.add(d)

    # AdditionalDependencies (libs)
    for n in root.findall(".//ns:AdditionalDependencies", NS):
        if not n.text:
            continue
        for l in n.text.split(";"):
            lib = l.strip()
            if not lib or not lib.endswith(".lib"):
                continue
            lib_name = Path(lib).stem

            # пропускаем системные Windows библиотеки
            if lib_name.lower() in WIN_LIBS:
                continue

            # проверка на Qt через регулярное выражение
            m = QT_REGEX.fullmatch(lib_name)
            if m:
                module = m.group(1)
                proj.libs.add(f"Qt${{QT_VERSION_MAJOR}}::{module}")
            else:
                proj.libs.add(lib_name)

    # QtModules (<QtModules>core;gui;widgets;xml;charts;printsupport</QtModules>)
    for n in root.findall(".//ns:QtModules", NS):
        if not n.text:
            continue
        for module in n.text.split(";"):
            module = module.strip()
            if module.lower() in QT_MODULES_MAP:
                proj.libs.add(f"Qt${{QT_VERSION_MAJOR}}::{QT_MODULES_MAP[module.lower()]}")

    # ProjectReference
    for n in root.findall(".//ns:ProjectReference", NS):
        ref = n.get("Include")
        if ref:
            proj.deps.add((proj_dir / ref).resolve().stem)

    # ConfigurationType
    conf = root.find(".//ns:ConfigurationType", NS)
    if conf is not None:
        t = conf.text.lower()
        if "static" in t:
            proj.type = "static"
        elif "dynamic" in t:
            proj.type = "shared"

    # AdditionalOptions (compile flags)
    for n in root.findall(".//ns:AdditionalOptions", NS):
        if n.text:
            proj.cflags.add(n.text)

    # filters
    parse_filters(proj)

    return proj
    
def parse_sln(path):
    projects = {}
    project_re = re.compile(r'Project\(".*"\)\s=\s"([^"]+)",\s"([^"]+)",')
    with open(path, encoding="utf8", errors="ignore") as f:
        for line in f:
            m = project_re.search(line)
            if not m:
                continue
            name = m.group(1)
            proj_path = m.group(2)
            if proj_path.endswith(".vcxproj"):
                projects[name] = proj_path.replace("\\", "/")
    return projects

def get_project_name(vcx_path: Path):
    try:
        tree = ET.parse(vcx_path)
        root = tree.getroot()
        name_node = root.find(".//ns:ProjectName", NS)
        if name_node is not None and name_node.text:
            return name_node.text.strip()
    except Exception:
        pass
    return vcx_path.stem

def get_solution_name(sln_path: Path):
    # просто имя файла без расширения
    return sln_path.stem

def detect_qt(proj: Project):
    for s in proj.sources + proj.headers + proj.ui_files + proj.resources:
        if s.suffix in (".ui", ".qrc"):
            return True
    for inc in proj.includes:
        if "Qt" in str(inc):
            return True
    for l in proj.libs:
        if l.lower().startswith("qt"):
            return True
    return False

def write_project_cmake(proj: Project):
    proj_dir = proj.path.parent
    cmake = proj_dir / "CMakeLists.txt"

    src_files = [safe_rel(f, proj_dir) for f in proj.sources]
    hdr_files = [safe_rel(f, proj_dir) for f in proj.headers]
    qm_files = [safe_rel(f, proj_dir) for f in proj.qm_files]
    ui_files = [safe_rel(f, proj_dir) for f in proj.ui_files]
    rc_files = [safe_rel(f, proj_dir) for f in proj.resources]
    img_files = [safe_rel(f, proj_dir) for f in proj.images]
    include_dirs = [safe_rel(f, proj_dir) for f in proj.includes]

    with open(cmake, "w", encoding="utf8") as f:
        f.write(f"# generated from {proj.path.name}\n\n")
        f.write(f"project({proj.name})\n")
        f.write("message(STATUS \"- Prepare: ${PROJECT_NAME}\")\n\n")

        # Переменные
        if src_files:
            f.write("set(SOURCE_FILES\n")
            for s in sorted(src_files):
                f.write(f"    {quote_if_needed(s)}\n")
            f.write(")\n\n")

        if hdr_files:
            f.write("set(HEADER_FILES\n")
            for h in sorted(hdr_files):
                f.write(f"    {quote_if_needed(h)}\n")
            f.write(")\n\n")

        if qm_files:
            f.write("set(QM_FILES\n")
            for q in sorted(qm_files):
                f.write(f"    {quote_if_needed(q)}\n")
            f.write(")\n\n")

        if ui_files:
            f.write("set(UI_FILES\n")
            for q in sorted(ui_files):
                f.write(f"    {quote_if_needed(q)}\n")
            f.write(")\n\n")

        if rc_files:
            f.write("set(RESOURCE_FILES\n")
            for r in sorted(rc_files):
                f.write(f"    {quote_if_needed(r)}\n")
            f.write(")\n\n")

        if img_files:
            f.write("set(IMAGE_FILES\n")
            for i in sorted(img_files):
                f.write(f"    {quote_if_needed(i)}\n")
            f.write(")\n\n")

        # Include directories
        if proj.includes:
            f.write("include_directories(\n")
            for inc in sorted(proj.includes):
                f.write(f"    {quote_if_needed(format_include(inc, proj_dir))}\n")
            f.write(")\n\n")

        # source_group для IDE
        if src_files:
            f.write("source_group(\"Source Files\" FILES ${SOURCE_FILES})\n")
        if hdr_files:
            f.write("source_group(\"Header Files\" FILES ${HEADER_FILES})\n")
        if qm_files:
            f.write("source_group(\"QRC Files\" FILES ${QM_FILES})\n")
        if ui_files:
            f.write("source_group(\"Form Files\" FILES ${UI_FILES})\n")
        if rc_files:
            f.write("source_group(\"Resource Files\" FILES ${RESOURCE_FILES})\n")
        if img_files:
            f.write("source_group(\"Images\" FILES ${IMAGE_FILES})\n")
        f.write("\n")

        # add_executable / add_library с включением всех файлов
        all_files = ["${SOURCE_FILES}", "${HEADER_FILES}"]
        if ui_files:
            all_files.append("${UI_FILES}")
        if qm_files:
            all_files.append("${QM_FILES}")
        if rc_files:
            all_files.append("${RESOURCE_FILES}")
        if img_files:
            all_files.append("${IMAGE_FILES}")

        files_str = "\n    ".join(all_files)

        if proj.type == "exe":
            f.write("add_executable(${PROJECT_NAME}\n")
        elif proj.type == "static":
            f.write("add_library(${PROJECT_NAME} STATIC\n")
        else:
            f.write("add_library(${PROJECT_NAME} SHARED\n")
        f.write(f"    {files_str}\n)\n")

        # target_link_libraries для зависимостей
        if proj.deps or proj.libs:
            f.write("\ntarget_link_libraries(${PROJECT_NAME}\n")
            for d in sorted(set(proj.deps)):
                f.write(f"    {quote_if_needed(d)}\n")
            for l in sorted(set(proj.libs)):
                f.write(f"    {quote_if_needed(l)}\n")
            f.write(")\n")

    print("generated:", cmake)
    
def write_root_cmake(root, sln, projects):

    cmake = root / "CMakeLists.txt"

    qt_components = set()
    for p in projects:
        # собираем компоненты из libs
        for l in p.libs:
            if l.startswith("Qt${QT_VERSION_MAJOR}::"):
                comp = l.split("::")[1]
                qt_components.add(comp)

    with open(cmake, "w", encoding="utf8") as f:

        f.write("cmake_minimum_required(VERSION 3.20)\n\n")

        solution_name = get_solution_name(sln)
        f.write(f'project("{solution_name}")\n')
        f.write('message(STATUS "Prepare: ${PROJECT_NAME}")\n\n')

        f.write("set(CMAKE_CXX_STANDARD 20)\n")
        f.write("set(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n")

        if qt_components:
            f.write('set(FIND_QT_DIR "/usr/include/x86_64-linux-gnu/qt5/" CACHE PATH "Set qt dir here")\n')
            f.write('set(CMAKE_PREFIX_PATH ${FIND_QT_DIR})\n')
            components_str = " ".join(sorted(qt_components))
            f.write(f'find_package(QT COMPONENTS {components_str} NAMES Qt6 Qt5 REQUIRED)\n')
            f.write(f'find_package(Qt${{QT_VERSION_MAJOR}} REQUIRED COMPONENTS {components_str})\n\n')

            f.write(f'if(NOT Qt${{QT_VERSION_MAJOR}}_FOUND)\n')
            f.write(f'    message(FATAL_ERROR "Qt${{QT_VERSION_MAJOR}} not found!")\n')
            f.write("endif()\n\n")
            f.write(f'message(STATUS "Found Qt${{QT_VERSION_MAJOR}} version ${{Qt${{QT_VERSION_MAJOR}}_VERSION}}")\n\n')
                                                                      
            f.write("set(CMAKE_AUTOMOC ON)\n")
            f.write("set(CMAKE_AUTOUIC ON)\n")
            f.write("set(CMAKE_AUTORCC ON)\n\n")

        for p in sorted(projects, key=lambda x: x.path.parent.relative_to(root)):

            rel = p.path.parent.relative_to(root)
            f.write(f'add_subdirectory({quote_if_needed(rel.as_posix())})\n')

    print("generated:", cmake)

def main():
    parser = argparse.ArgumentParser(description="Convert MSVC solution to CMake")
    parser.add_argument("path", nargs="?", help="Path to solution file (.sln) or directory")
    args = parser.parse_args()

    if args.path:
        path = Path(args.path).resolve()
        if path.is_file() and path.suffix.lower() == ".sln":
            # Передан конкретный sln-файл
            sln = path
            root = sln.parent
        elif path.is_dir():
            # Передана директория — ищем sln внутри
            sln_files = list(path.rglob("*.sln"))
            if not sln_files:
                print("No solution file found in directory:", path)
                return
            sln = sln_files[0]
            root = path
        else:
            print("Invalid path:", path)
            return
    else:
        # По умолчанию текущая директория
        root = Path(".").resolve()
        sln_files = list(root.rglob("*.sln"))
        if not sln_files:
            print("No solution file found in current directory")
            return
        sln = sln_files[0]

    sln_dir = sln.parent
    print("Solution:", sln)

    # Парсим проекты из sln
    sln_projects = parse_sln(sln)
    projects = []

    for name, rel_path in sln_projects.items():
        vcx = (sln_dir / rel_path).resolve()
        if not vcx.exists():
            print("skip missing:", vcx)
            continue
        proj = parse_vcxproj(vcx, sln_dir)
        projects.append(proj)

    # Генерируем CMakeLists.txt для каждого проекта
    for p in projects:
        write_project_cmake(p)

    # Генерируем корневой CMakeLists.txt
    write_root_cmake(root, sln, projects)

if __name__ == "__main__":
    main()