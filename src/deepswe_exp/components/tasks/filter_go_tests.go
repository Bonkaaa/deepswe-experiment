package main

import (
	"encoding/json"
	"flag"
	"bytes"
	"fmt"
	"go/format"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type edit struct {
	start int
	end   int
	text  string
}

func main() {
	root := flag.String("root", "/testbed", "repository root")
	pkg := flag.String("pkg", ".", "target Go package path")
	pattern := flag.String("pattern", "^Test.*Router", "in-scope test names")
	keepPath := flag.String("keep", "", "JSON list of retained test names")
	flag.Parse()

	if *keepPath == "" {
		fail("missing -keep")
	}
	patternRE, err := regexp.Compile(*pattern)
	if err != nil {
		fail("invalid test pattern: %v", err)
	}
	keepData, err := os.ReadFile(*keepPath)
	if err != nil {
		fail("read retained test names: %v", err)
	}
	var names []string
	if err := jsonUnmarshal(keepData, &names); err != nil {
		fail("decode retained test names: %v", err)
	}
	kept := make(map[string]bool, len(names))
	for _, name := range names {
		kept[name] = true
	}

	command := exec.Command("go", "list", "-json", *pkg)
	command.Dir = *root
	packageData, err := command.CombinedOutput()
	if err != nil {
		fail("list active Go package files: %s", strings.TrimSpace(string(packageData)))
	}
	var packageFiles struct {
		Dir          string
		TestGoFiles  []string
		XTestGoFiles []string
	}
	if err := json.Unmarshal(packageData, &packageFiles); err != nil {
		fail("decode active Go package files: %v", err)
	}
	pkgDir := packageFiles.Dir
	if pkgDir == "" {
		pkgDir = *root
	}
	files := append(packageFiles.TestGoFiles, packageFiles.XTestGoFiles...)
	if len(files) == 0 {
		fail("find active Go test files")
	}
	for _, file := range files {
		path := filepath.Join(pkgDir, file)
		if err := filterFile(path, patternRE, kept); err != nil {
			fail("filter %s: %v", path, err)
		}
	}
	if err := pruneUnusedImports(*root, *pkg); err != nil {
		fail("prune unused Go imports: %v", err)
	}
}

func filterFile(path string, pattern *regexp.Regexp, kept map[string]bool) error {
	fileSet := token.NewFileSet()
	file, err := parser.ParseFile(fileSet, path, nil, parser.ParseComments)
	if err != nil {
		return err
	}
	original, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	position := func(value token.Pos) int { return fileSet.Position(value).Offset }

	edits := make([]edit, 0)
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if ok && function.Recv == nil && pattern.MatchString(function.Name.Name) && !kept[function.Name.Name] {
			start := position(function.Pos())
			if function.Doc != nil {
				start = position(function.Doc.Pos())
			}
			end := position(function.End())
			for end < len(original) && (original[end] == ' ' || original[end] == '\t') {
				end++
			}
			if end < len(original) && original[end] == ';' {
				end++
				for end < len(original) && (original[end] == ' ' || original[end] == '\t') {
					end++
				}
			}
			edits = append(edits, edit{start: start, end: end})
			continue
		}
	}

	sort.Slice(edits, func(i, j int) bool { return edits[i].start > edits[j].start })
	for _, change := range edits {
		original = append(original[:change.start], append([]byte(change.text), original[change.end:]...)...)
	}
	return os.WriteFile(path, original, 0644)
}

var unusedImportDiagnostic = regexp.MustCompile(`(?m)^(.+\.go):(\d+):\d+: "([^"]+)" imported(?: as [^ ]+)? and not used$`)

type importLocation struct {
	line int
	path string
}

func pruneUnusedImports(root string, pkg string) error {
	for attempt := 0; attempt < 100; attempt++ {
		command := exec.Command("go", "test", "-c", "-o", "/tmp/deepswe-filter-check.test", pkg)
		command.Dir = root
		output, err := command.CombinedOutput()
		if err == nil {
			return nil
		}
		matches := unusedImportDiagnostic.FindAllStringSubmatch(string(output), -1)
		if len(matches) == 0 {
			return fmt.Errorf("filtered package does not compile: %s", strings.TrimSpace(string(output)))
		}
		byFile := make(map[string][]importLocation)
		for _, match := range matches {
			line, parseErr := strconv.Atoi(match[2])
			if parseErr != nil {
				return fmt.Errorf("parse unused-import location %q: %w", match[0], parseErr)
			}
			byFile[match[1]] = append(byFile[match[1]], importLocation{line: line, path: match[3]})
		}
		files := make([]string, 0, len(byFile))
		for file := range byFile {
			files = append(files, file)
		}
		sort.Strings(files)
		removed := 0
		for _, file := range files {
			path := filepath.Join(root, strings.TrimPrefix(file, "./"))
			count, err := removeImportsAtLocations(path, byFile[file])
			if err != nil {
				return err
			}
			removed += count
		}
		if removed == 0 {
			return fmt.Errorf("compiler reported unused imports but none could be removed: %s", string(output))
		}
	}
	return fmt.Errorf("too many unused-import pruning passes")
}

func removeImportsAtLocations(path string, locations []importLocation) (int, error) {
	fileSet := token.NewFileSet()
	file, err := parser.ParseFile(fileSet, path, nil, parser.ParseComments)
	if err != nil {
		return 0, err
	}
	targets := make(map[string]bool, len(locations))
	for _, location := range locations {
		targets[fmt.Sprintf("%d:%s", location.line, location.path)] = true
	}
	removed := 0
	removedComments := make(map[*ast.CommentGroup]bool)
	for _, declaration := range file.Decls {
		group, ok := declaration.(*ast.GenDecl)
		if !ok || group.Tok != token.IMPORT {
			continue
		}
		kept := make([]ast.Spec, 0, len(group.Specs))
		for _, declaration := range group.Specs {
			spec := declaration.(*ast.ImportSpec)
			actualPath, unquoteErr := strconv.Unquote(spec.Path.Value)
			if unquoteErr != nil {
				return 0, unquoteErr
			}
			line := fileSet.Position(spec.Pos()).Line
			key := fmt.Sprintf("%d:%s", line, actualPath)
			if !targets[key] {
				kept = append(kept, spec)
				continue
			}
			if spec.Doc != nil {
				removedComments[spec.Doc] = true
			}
			if spec.Comment != nil {
				removedComments[spec.Comment] = true
			}
			removed++
			delete(targets, key)
		}
		group.Specs = kept
	}
	if len(targets) != 0 {
		return 0, fmt.Errorf("unused-import diagnostics point to no matching imports in %s: %v", path, targets)
	}
	if removed == 0 {
		return 0, nil
	}
	comments := file.Comments[:0]
	for _, comment := range file.Comments {
		if !removedComments[comment] {
			comments = append(comments, comment)
		}
	}
	file.Comments = comments
	declarations := file.Decls[:0]
	for _, declaration := range file.Decls {
		if group, ok := declaration.(*ast.GenDecl); ok && group.Tok == token.IMPORT && len(group.Specs) == 0 {
			continue
		}
		declarations = append(declarations, declaration)
	}
	file.Decls = declarations
	var formatted bytes.Buffer
	if err := format.Node(&formatted, fileSet, file); err != nil {
		return 0, err
	}
	if err := os.WriteFile(path, formatted.Bytes(), 0644); err != nil {
		return 0, err
	}
	return removed, nil
}

func jsonUnmarshal(data []byte, destination any) error {
	return json.Unmarshal(data, destination)
}

func fail(format string, arguments ...any) {
	fmt.Fprintf(os.Stderr, strings.TrimSpace(format)+"\n", arguments...)
	os.Exit(2)
}
