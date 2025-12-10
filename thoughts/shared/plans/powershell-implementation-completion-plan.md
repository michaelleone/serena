# PowerShell Language Support Completion Plan

## Status Summary

The PowerShell language support has been implemented and committed with the following components:
- `src/solidlsp/language_servers/powershell_language_server.py` - LSP implementation
- `src/solidlsp/ls_config.py` - Language enum and file matchers
- `test/resources/repos/powershell/test_repo/` - Test repository (main.ps1, utils.ps1)
- `test/solidlsp/powershell/test_powershell_basic.py` - Basic tests (5 tests passing)
- `pyproject.toml` - Added `powershell` pytest marker

## Outstanding Tasks (Compliance Gaps)

Based on the GPT-5.1 analysis against `.serena/memories/adding_new_language_support_guide.md`, the following gaps need to be addressed:

---

### Phase 1: Reference Tests (HIGH Priority)

The guide requires testing:
1. Finding within-file references
2. Finding cross-file references

**Current State**: The test repo already has cross-file dependencies (main.ps1 imports utils.ps1 and calls `Convert-ToUpperCase`, `Remove-Whitespace`), but no reference-finding tests exist.

#### Task 1.1: Add Within-File Reference Test

Add to `test/solidlsp/powershell/test_powershell_basic.py`:

```python
@pytest.mark.parametrize("language_server", [Language.POWERSHELL], indirect=True)
def test_powershell_find_references_within_file(self, language_server: SolidLanguageServer) -> None:
    """Test finding references to a function within the same file."""
    main_path = "main.ps1"

    # Get symbols to find the Main function which calls Greet-User
    all_symbols, _root_symbols = language_server.request_document_symbols(main_path).get_all_symbols_and_roots()

    # Find Greet-User function definition
    function_symbols = [s for s in all_symbols if s.get("kind") == 12]
    greet_user_symbol = next((s for s in function_symbols if "Greet-User" in s["name"]), None)
    assert greet_user_symbol is not None, f"Should find Greet-User function"

    # Find references to Greet-User (should be called from Main)
    sel_start = greet_user_symbol["selectionRange"]["start"]
    refs = language_server.request_references(main_path, sel_start["line"], sel_start["character"])

    # Should find at least the call site in Main function (line 91: Greet-User -Username $User -GreetingType $Greeting)
    assert refs is not None and len(refs) >= 1, f"Should find references to Greet-User, got {refs}"
    assert any("main.ps1" in ref.get("uri", ref.get("relativePath", "")) for ref in refs), \
        f"Should find reference in main.ps1, got {refs}"
```

#### Task 1.2: Add Cross-File Reference Test

Add test for cross-file references:

```python
@pytest.mark.parametrize("language_server", [Language.POWERSHELL], indirect=True)
def test_powershell_find_references_across_files(self, language_server: SolidLanguageServer) -> None:
    """Test finding references to functions across files."""
    utils_path = "utils.ps1"

    # Get symbols from utils.ps1 to find Convert-ToUpperCase
    all_symbols, _root_symbols = language_server.request_document_symbols(utils_path).get_all_symbols_and_roots()

    function_symbols = [s for s in all_symbols if s.get("kind") == 12]
    convert_symbol = next((s for s in function_symbols if "Convert-ToUpperCase" in s["name"]), None)
    assert convert_symbol is not None, f"Should find Convert-ToUpperCase function"

    # Find references - should include call from main.ps1 (line 99: Convert-ToUpperCase -InputString $User)
    sel_start = convert_symbol["selectionRange"]["start"]
    refs = language_server.request_references(utils_path, sel_start["line"], sel_start["character"])

    # Check that reference from main.ps1 is found
    assert refs is not None and len(refs) >= 1, f"Should find references to Convert-ToUpperCase, got {refs}"
    main_refs = [ref for ref in refs if "main.ps1" in ref.get("uri", ref.get("relativePath", ""))]
    assert len(main_refs) >= 1, f"Should find cross-file reference in main.ps1, got {refs}"
```

**Note**: PSES reference finding capability may require investigation. If references are not working, this should be documented and potentially flagged as a known limitation.

---

### Phase 2: Integration Tests (HIGH Priority)

Add PowerShell to `test/serena/test_serena_agent.py` parametrized tests.

#### Task 2.1: Add to test_find_symbol

Add to the `test_find_symbol` parameters:

```python
pytest.param(Language.POWERSHELL, "Greet-User", "Function", "main.ps1", marks=pytest.mark.powershell),
```

**Note**: PSES returns function names as "function Greet-User ()" - may need to adjust matching or use a different expected name format.

#### Task 2.2: Add to test_find_symbol_references

Add to the `test_find_symbol_references` parameters:

```python
pytest.param(
    Language.POWERSHELL,
    "Convert-ToUpperCase",
    "utils.ps1",
    "main.ps1",
    marks=pytest.mark.powershell,
),
```

#### Task 2.3: Add PowerShell to serena_config fixture

Add `Language.POWERSHELL` to the list of test projects in the `serena_config` fixture:

```python
for language in [
    Language.PYTHON,
    Language.GO,
    Language.JAVA,
    Language.KOTLIN,
    Language.RUST,
    Language.TYPESCRIPT,
    Language.PHP,
    Language.CSHARP,
    Language.CLOJURE,
    Language.POWERSHELL,  # Add this
]:
```

---

### Phase 3: Documentation Updates (MEDIUM Priority)

#### Task 3.1: Update README.md

Update line 71 in README.md to include PowerShell in the supported languages list:

**Current**:
```
AL, Bash, C#, C/C++, Clojure, Dart, Elixir, Elm, Erlang, Fortran, Go, Haskell, Java, Javascript, Julia, Kotlin, Lua, Markdown, Nix, Perl, PHP, Python, R, Ruby, Rust, Scala, Swift, TypeScript, YAML and Zig.
```

**Updated**:
```
AL, Bash, C#, C/C++, Clojure, Dart, Elixir, Elm, Erlang, Fortran, Go, Haskell, Java, Javascript, Julia, Kotlin, Lua, Markdown, Nix, Perl, PHP, PowerShell, Python, R, Ruby, Rust, Scala, Swift, TypeScript, YAML and Zig.
```

#### Task 3.2: Update CHANGELOG.md

Add to the `# latest` section under `* Language support:`:

```markdown
  * **Add support for PowerShell** via PowerShell Editor Services (PSES). Requires PowerShell Core (pwsh) to be installed.
```

---

## Implementation Order

1. **Phase 1: Reference Tests** - Most critical for guide compliance
   - [ ] Add within-file reference test
   - [ ] Add cross-file reference test
   - [ ] Verify PSES reference capabilities work (or document limitations)

2. **Phase 2: Integration Tests** - Required by guide
   - [ ] Add PowerShell to serena_config fixture
   - [ ] Add test_find_symbol parameter
   - [ ] Add test_find_symbol_references parameter

3. **Phase 3: Documentation** - Required by guide
   - [ ] Update README.md language list
   - [ ] Update CHANGELOG.md with new language entry

---

## Success Criteria

1. All new tests pass: `uv run poe test -m powershell`
2. Integration tests pass: `uv run pytest test/serena/test_serena_agent.py -m powershell -v`
3. Formatting/type checking pass: `uv run poe format && uv run poe type-check`
4. README.md includes PowerShell in language list
5. CHANGELOG.md documents PowerShell support

---

## Potential Issues

1. **PSES Reference Finding**: PowerShell Editor Services may have limited cross-file reference support. If tests fail, we may need to:
   - Document this as a known limitation
   - Skip reference tests with appropriate reason
   - Investigate PSES configuration for workspace-wide references

2. **Symbol Name Format**: PSES returns "function Greet-User ()" format. Integration tests may need adjusted assertions to handle this format.

3. **CI Dependencies**: Need to ensure PowerShell Core (`pwsh`) is available in CI environment. May need to add workflow step to install it.
