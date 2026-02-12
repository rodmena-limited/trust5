import glob as _glob
import os
from dataclasses import asdict, dataclass
from typing import Any
_COMMON_SKIP = (".moai", ".trust5", ".git")
PROFILES: dict[str, LanguageProfile] = {
    "python": _p(
        "python",
        (".py",),
        ("python3", "-m", "pytest", "-v", "--tb=long", "-x"),
        'Bash("pytest -v --tb=short")',
        ("python3 -m ruff check --fix . 2>/dev/null", "python3 -m black . 2>/dev/null"),
        ("python3", "-m", "compileall", "-q", "."),
        "pip install",
        "python",
        ("__pycache__", ".venv", "venv", ".tox", ".nox", ".eggs"),
        ("pyproject.toml", "requirements.txt", "setup.py"),
        "Language: Python. Use pytest for testing. Follow PEP 8. "
        "Use argparse or click for CLI. Use the project venv for all commands.",
        ("python3", "-m", "pytest", "--cov=.", "--cov-report=term-missing", "-q", "--ignore=.venv", "--ignore=venv"),
        (
            "python3",
            "-m",
            "bandit",
            "-r",
            ".",
            "-q",
            "-f",
            "json",
            "--exclude",
            ".venv,venv,.tox,.nox,.eggs,tests,test",
        ),
        ("FastAPI", "Django", "Flask"),
        lint_check=("python3 -m ruff check --output-format=concise .",),
        src_roots=("src", "lib"),
        path_var="PYTHONPATH",
    ),
    "go": _p(
        "go",
        (".go",),
        ("go", "test", "-v", "-race", "./..."),
        'Bash("go test -v ./...")',
        ("gofmt -w . 2>/dev/null", "go vet ./... 2>/dev/null"),
        ("go", "vet", "./..."),
        "go get",
        "go",
        ("vendor",),
        ("go.mod", "go.sum"),
        "Language: Go. Use testing package. Table-driven tests. cobra for CLI.",
        ("go", "test", "-coverprofile=coverage.out", "-covermode=atomic", "./..."),
        ("gosec", "-fmt=json", "-quiet", "-exclude-dir=vendor", "./..."),
        ("Gin", "Echo", "Fiber", "Chi"),
        lint_check=("gofmt -l .", "go vet ./... 2>&1"),
    ),
    "typescript": _p(
        "typescript",
        (".ts", ".tsx"),
        ("npx", "jest", "--verbose"),
        'Bash("npx jest --verbose")',
        ("npx eslint --fix . 2>/dev/null", "npx prettier --write . 2>/dev/null"),
        ("npx", "tsc", "--noEmit"),
        "npm install",
        "typescript",
        ("node_modules", "dist", ".next"),
        ("package.json", "tsconfig.json"),
        "Language: TypeScript. Use Jest or Vitest. ESLint + strict mode.",
        ("npx", "jest", "--coverage", "--coverageReporters=text"),
        ("npx", "audit-ci", "--moderate"),
        ("Next.js", "React", "NestJS", "Express"),
        lint_check=("npx eslint --format=unix .",),
    ),
    "javascript": _p(
        "javascript",
        (".js", ".jsx", ".mjs", ".cjs"),
        ("npx", "jest", "--verbose"),
        'Bash("npx jest --verbose")',
        ("npx eslint --fix . 2>/dev/null", "npx prettier --write . 2>/dev/null"),
        None,
        "npm install",
        "javascript",
        ("node_modules", "dist"),
        ("package.json",),
        "Language: JavaScript ES2024+. Use Jest or Vitest. ESLint rules.",
        ("npx", "jest", "--coverage", "--coverageReporters=text"),
        ("npx", "audit-ci", "--moderate"),
        ("React", "Vue", "Express", "Fastify"),
        lint_check=("npx eslint --format=unix .",),
    ),
    "rust": _p(
        "rust",
        (".rs",),
        ("cargo", "test", "--", "--nocapture"),
        'Bash("cargo test")',
        ("cargo fmt 2>/dev/null", "cargo clippy --fix --allow-dirty 2>/dev/null"),
        ("cargo", "check"),
        "cargo add",
        "rust",
        ("target",),
        ("Cargo.toml", "Cargo.lock"),
        "Language: Rust. Use #[test] + cargo test. clippy lints. clap for CLI.",
        ("cargo", "tarpaulin", "--out", "stdout", "--skip-clean"),
        ("cargo", "audit"),
        ("Actix", "Axum", "Rocket"),
        lint_check=("cargo fmt --check 2>&1", "cargo clippy 2>&1"),
    ),
    "java": _p(
        "java",
        (".java",),
        ("mvn", "test", "-q"),
        'Bash("mvn test -q")',
        ("mvn spotless:apply 2>/dev/null",),
        ("mvn", "compile", "-q"),
        "mvn dependency:resolve",
        "java",
        ("target", "build", ".gradle"),
        ("pom.xml", "build.gradle", "build.gradle.kts"),
        "Language: Java. JUnit 5 for testing. Google Java Style. Maven/Gradle.",
        ("mvn", "jacoco:report", "-q"),
        ("mvn", "spotbugs:check", "-q"),
        ("Spring Boot", "Quarkus"),
        lint_check=("mvn spotless:check -q 2>&1",),
    ),
    "ruby": _p(
        "ruby",
        (".rb",),
        ("bundle", "exec", "rspec", "--format", "documentation"),
        'Bash("bundle exec rspec")',
        ("bundle exec rubocop -A 2>/dev/null",),
        None,
        "gem install",
        "ruby",
        (".bundle", "vendor"),
        ("Gemfile", "Gemfile.lock"),
        "Language: Ruby. RSpec for testing. RuboCop style.",
        ("bundle", "exec", "rspec", "--format", "progress"),
        ("bundle", "exec", "brakeman", "-q", "--no-pager"),
        ("Rails", "Sinatra"),
        lint_check=("bundle exec rubocop --format=emacs 2>&1",),
    ),
    "elixir": _p(
        "elixir",
        (".ex", ".exs"),
        ("mix", "test", "--trace"),
        'Bash("mix test")',
        ("mix format 2>/dev/null", "mix credo --strict 2>/dev/null"),
        ("mix", "compile", "--warnings-as-errors"),
        "mix deps.get",
        "elixir",
        ("_build", "deps"),
        ("mix.exs", "mix.lock"),
        "Language: Elixir. ExUnit for testing. mix format.",
        ("mix", "test", "--cover"),
        ("mix", "sobelow", "--format", "json"),
        ("Phoenix",),
        lint_check=("mix format --check-formatted 2>&1", "mix credo --strict --format=oneline 2>&1"),
    ),
    "cpp": _p(
        "cpp",
        (".cpp", ".cc", ".cxx", ".h", ".hpp"),
        ("cmake", "--build", "build", "--target", "test"),
        'Bash("cmake --build build --target test")',
        ("clang-format -i **/*.cpp **/*.h 2>/dev/null",),
        ("cmake", "--build", "build"),
        "conan install",
        "cpp",
        ("build", "cmake-build-debug"),
        ("CMakeLists.txt", "conanfile.txt"),
        "Language: C++. Google Test/Catch2. clang-format. CMake.",
        ("cmake", "--build", "build", "--target", "coverage"),
        ("cppcheck", "--enable=all", "--error-exitcode=1", "."),
        lint_check=("clang-format --dry-run --Werror **/*.cpp **/*.h 2>&1",),
    ),
    "c": _p(
        "c",
        (".c", ".h"),
        ("ctest",),
        'Bash("ctest")',
        ("clang-format -i **/*.c **/*.h 2>/dev/null",),
        ("make",),
        "apt install",
        "c",
        ("build",),
        ("Makefile", "CMakeLists.txt"),
        "Language: C. Use CUnit or Check. CMake or Make build system.",
        None,
        ("cppcheck", "--enable=all", "--error-exitcode=1", "."),
        lint_check=("clang-format --dry-run --Werror **/*.c **/*.h 2>&1",),
    ),
    "php": _p(
        "php",
        (".php",),
        ("vendor/bin/phpunit",),
        'Bash("vendor/bin/phpunit")',
        ("vendor/bin/php-cs-fixer fix . 2>/dev/null",),
        ("php", "-l"),
        "composer require",
        "php",
        ("vendor",),
        ("composer.json", "composer.lock"),
        "Language: PHP 8.3+. PHPUnit for testing. PHP-CS-Fixer.",
        ("vendor/bin/phpunit", "--coverage-text"),
        None,  # phpstan is a type checker, not a security scanner
        ("Laravel", "Symfony"),
        lint_check=("vendor/bin/php-cs-fixer fix --dry-run --diff 2>&1",),
    ),
    "kotlin": _p(
        "kotlin",
        (".kt", ".kts"),
        ("gradle", "test"),
        'Bash("gradle test")',
        (
            "gradle",
            "ktlintFormat 2>/dev/null",
        ),
        ("gradle", "compileKotlin"),
        "gradle dependencies",
        "kotlin",
        ("build", ".gradle"),
        ("build.gradle.kts", "build.gradle"),
        "Language: Kotlin 2.0+. JUnit 5. Gradle. ktlint.",
        ("gradle", "test", "jacocoTestReport"),
        ("gradle", "detekt"),
        ("Ktor", "Spring Boot"),
        lint_check=("gradle ktlintCheck 2>&1",),
    ),
    "swift": _p(
        "swift",
        (".swift",),
        ("swift", "test"),
        'Bash("swift test")',
        (
            "swift-format",
            "format",
            "-i",
            "-r",
            ". 2>/dev/null",
        ),
        ("swift", "build"),
        "swift package resolve",
        "swift",
        (".build",),
        ("Package.swift",),
        "Language: Swift 6+. XCTest. SwiftPM.",
        ("swift", "test", "--enable-code-coverage"),
        None,
        ("SwiftUI", "Vapor"),
        lint_check=("swift-format lint -r . 2>&1",),
    ),
    "dart": _p(
        "dart",
        (".dart",),
        ("dart", "test"),
        'Bash("dart test")',
        (
            "dart",
            "fix",
            "--apply 2>/dev/null",
        ),
        ("dart", "analyze"),
        "dart pub add",
        "dart",
        (".dart_tool", "build"),
        ("pubspec.yaml", "pubspec.lock"),
        "Language: Dart 3.5+. Use dart test. dart fix for formatting.",
        ("dart", "test", "--coverage"),
        None,
        ("Flutter",),
        lint_check=("dart analyze 2>&1",),
    ),
    "scala": _p(
        "scala",
        (".scala", ".sc"),
        ("sbt", "test"),
        'Bash("sbt test")',
        (
            "sbt",
            "scalafmtAll 2>/dev/null",
        ),
        ("sbt", "compile"),
        "sbt update",
        "scala",
        ("target", "project/target"),
        ("build.sbt",),
        "Language: Scala 3. ScalaTest. sbt. scalafmt.",
        ("sbt", "coverage", "test", "coverageReport"),
        None,
        ("Akka", "Cats Effect", "ZIO"),
        lint_check=("sbt scalafmtCheckAll 2>&1",),
    ),
    "haskell": _p(
        "haskell",
        (".hs",),
        ("cabal", "test"),
        'Bash("cabal test")',
        (
            "ormolu",
            "-i **/*.hs 2>/dev/null",
        ),
        ("cabal", "build"),
        "cabal install",
        "haskell",
        ("dist-newstyle",),
        ("cabal.project", "package.yaml", "stack.yaml"),
        "Language: Haskell. HSpec or tasty. cabal.",
        ("cabal", "test", "--enable-coverage"),
        None,
        lint_check=("ormolu --check **/*.hs 2>&1",),
    ),
    "zig": _p(
        "zig",
        (".zig",),
        ("zig", "test"),
        'Bash("zig test")',
        (),
        ("zig", "build"),
        "",
        "zig",
        ("zig-cache", "zig-out"),
        ("build.zig",),
        "Language: Zig. Use std.testing. zig build.",
        None,
        None,
    ),
    "r": _p(
        "r",
        (".R", ".r", ".Rmd"),
        ("Rscript", "-e", "testthat::test_dir('tests')"),
        "Bash(\"Rscript -e testthat::test_dir('tests')\")",
        (
            "Rscript",
            "-e",
            "styler::style_dir() 2>/dev/null",
        ),
        None,
        "Rscript -e install.packages",
        "r",
        ("renv",),
        ("DESCRIPTION", "NAMESPACE", "renv.lock"),
        "Language: R. testthat for testing. styler for formatting.",
        ("Rscript", "-e", "covr::package_coverage()"),
        None,
        ("Shiny",),
    ),
    "csharp": _p(
        "csharp",
        (".cs",),
        ("dotnet", "test"),
        'Bash("dotnet test")',
        (
            "dotnet",
            "format 2>/dev/null",
        ),
        ("dotnet", "build"),
        "dotnet add package",
        "csharp",
        ("bin", "obj"),
        ("*.csproj", "*.sln"),
        "Language: C# 12. xUnit/NUnit. dotnet CLI.",
        ("dotnet", "test", "--collect:XPlat Code Coverage"),
        ("dotnet", "tool", "run", "security-scan"),
        ("ASP.NET Core", "Blazor"),
        lint_check=("dotnet format --verify-no-changes 2>&1",),
    ),
    "lua": _p(
        "lua",
        (".lua",),
        ("busted",),
        'Bash("busted")',
        (),
        ("luac", "-p"),
        "luarocks install",
        "lua",
        (),
        ("*.rockspec",),
        "Language: Lua. busted for testing.",
        ("busted", "--coverage"),
        None,
    ),
    "html": _p(
        "html",
        (".html", ".htm"),
        (),
        'Bash("echo no tests")',
        ("npx prettier --write **/*.html 2>/dev/null",),
        None,
        "",
        "html",
        (),
        ("index.html",),
        "Language: HTML. Use semantic HTML5. Accessible markup.",
        None,
        None,
    ),
    "vue": _p(
        "vue",
        (".vue",),
        ("npx", "vitest", "run"),
        'Bash("npx vitest run")',
        ("npx eslint --fix . 2>/dev/null", "npx prettier --write . 2>/dev/null"),
        ("npx", "vue-tsc", "--noEmit"),
        "npm install",
        "vue",
        ("node_modules", "dist"),
        ("package.json", "vite.config.ts"),
        "Language: Vue 3.5. Vitest. ESLint + Prettier.",
        ("npx", "vitest", "--coverage"),
        None,
        ("Nuxt",),
        lint_check=("npx eslint --format=unix .",),
    ),
    "svelte": _p(
        "svelte",
        (".svelte",),
        ("npx", "vitest", "run"),
        'Bash("npx vitest run")',
        ("npx eslint --fix . 2>/dev/null", "npx prettier --write . 2>/dev/null"),
        ("npx", "svelte-check"),
        "npm install",
        "svelte",
        ("node_modules", ".svelte-kit"),
        ("package.json", "svelte.config.js"),
        "Language: Svelte. Vitest. SvelteKit.",
        ("npx", "vitest", "--coverage"),
        None,
        ("SvelteKit",),
        lint_check=("npx eslint --format=unix .",),
    ),
}
_FRAMEWORK_MARKERS: dict[str, str] = {
    "next.config.js": "Next.js",
    "next.config.mjs": "Next.js",
    "next.config.ts": "Next.js",
    "nuxt.config.ts": "Nuxt",
    "angular.json": "Angular",
    "svelte.config.js": "SvelteKit",
    "astro.config.mjs": "Astro",
    "remix.config.js": "Remix",
    "vite.config.ts": "Vite",
    "manage.py": "Django",
}
_MANIFEST_TO_LANG: dict[str, str] = {
    "nimble": "nim",
    "rebar.config": "erlang",
    "*.lpi": "pascal",
    "Makefile.PL": "perl",
    "cpanfile": "perl",
    "dune-project": "ocaml",
    "project.clj": "clojure",
    "deps.edn": "clojure",
    "JuliaProject.toml": "julia",
    "shard.yml": "crystal",
    "dub.json": "d",
    "dub.sdl": "d",
    "v.mod": "v",
    "gleam.toml": "gleam",
}
_EXT_TO_LANG: dict[str, str] = {}
_SKIP_DIRS_DETECT = frozenset(
    {
        ".git",
        ".moai",
        ".trust5",
        "node_modules",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
    }
)
_EXTENSION_MAP: dict[str, tuple[str, ...]] = {
    "nim": (".nim",),
    "erlang": (".erl", ".hrl"),
    "pascal": (".pas", ".pp"),
    "perl": (".pl", ".pm"),
    "ocaml": (".ml", ".mli"),
    "clojure": (".clj", ".cljs", ".cljc"),
    "groovy": (".groovy", ".gvy"),
    "fortran": (".f90", ".f95", ".f03"),
    "julia": (".jl",),
    "crystal": (".cr",),
    "d": (".d",),
    "v": (".v",),
    "gleam": (".gleam",),
    "odin": (".odin",),
}

def _p(
    lang: str,
    exts: tuple[str, ...],
    test: tuple[str, ...],
    verify: str,
    lint: tuple[str, ...],
    syntax: tuple[str, ...] | None,
    pkg: str,
    lsp_id: str,
    skip: tuple[str, ...],
    manifests: tuple[str, ...],
    hints: str,
    cov: tuple[str, ...] | None = None,
    sec: tuple[str, ...] | None = None,
    fw: tuple[str, ...] = (),
    lint_check: tuple[str, ...] = (),
    src_roots: tuple[str, ...] = (),
    path_var: str = "",
) -> LanguageProfile:
    return LanguageProfile(
        language=lang,
        extensions=exts,
        test_command=test,
        test_verify_command=verify,
        lint_commands=lint,
        lint_check_commands=lint_check,
        syntax_check_command=syntax,
        package_install_prefix=pkg,
        lsp_language_id=lsp_id,
        skip_dirs=skip + _COMMON_SKIP,
        manifest_files=manifests,
        prompt_hints=hints,
        coverage_command=cov,
        security_command=sec,
        frameworks=fw,
        source_roots=src_roots,
        path_env_var=path_var,
    )

def _manifest_exists(project_root: str, manifest: str) -> bool:
    """Check if a manifest file exists, supporting glob patterns (e.g. *.csproj)."""
    if "*" in manifest or "?" in manifest:
        return bool(_glob.glob(os.path.join(project_root, manifest)))
    return os.path.exists(os.path.join(project_root, manifest))

def _build_ext_map() -> None:
    """Lazily populate extension→language map from PROFILES."""
    if _EXT_TO_LANG:
        return
    for lang, profile in PROFILES.items():
        for ext in profile.extensions:
            _EXT_TO_LANG.setdefault(ext, lang)

def _detect_by_extensions(project_root: str) -> str:
    """Count source files by extension and return the dominant language."""
    _build_ext_map()
    counts: dict[str, int] = {}
    try:
        for entry in os.scandir(project_root):
            if entry.is_dir():
                if entry.name in _SKIP_DIRS_DETECT or entry.name.startswith("."):
                    continue
                # Scan one level deep for source files
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_file():
                            ext = os.path.splitext(sub.name)[1]
                            lang = _EXT_TO_LANG.get(ext)
                            if lang:
                                counts[lang] = counts.get(lang, 0) + 1
                except (PermissionError, OSError):
                    pass
            elif entry.is_file():
                ext = os.path.splitext(entry.name)[1]
                lang = _EXT_TO_LANG.get(ext)
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    except (PermissionError, FileNotFoundError, OSError):
        pass

    if not counts:
        return "unknown"
    return max(counts, key=counts.get)  # type: ignore[arg-type]

def detect_language(project_root: str) -> str:
    """Detect project language from manifest files, then file extensions."""
    # Primary: check for manifest/config files (most reliable signal)
    for lang, profile in PROFILES.items():
        for manifest in profile.manifest_files:
            if _manifest_exists(project_root, manifest):
                return lang
    for manifest, lang in _MANIFEST_TO_LANG.items():
        if _manifest_exists(project_root, manifest):
            return lang

    # Secondary: scan for source file extensions (helps when manifest files
    # haven't been created yet, e.g. pipeline build time before setup stage).
    return _detect_by_extensions(project_root)

@dataclass(frozen=True)
class LanguageProfile:
    language: str
    extensions: tuple[str, ...]
    test_command: tuple[str, ...]
    test_verify_command: str
    lint_commands: tuple[str, ...]
    syntax_check_command: tuple[str, ...] | None
    package_install_prefix: str
    lsp_language_id: str
    skip_dirs: tuple[str, ...]
    manifest_files: tuple[str, ...]
    prompt_hints: str
    lint_check_commands: tuple[str, ...] = ()
    coverage_command: tuple[str, ...] | None = None
    security_command: tuple[str, ...] | None = None
    frameworks: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    path_env_var: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
