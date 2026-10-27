// Private-at-runtime Roslyn helper. Roslyn assemblies are resolved from the installed SDK and never packaged.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.Diagnostics;
using Microsoft.CodeAnalysis.Text;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

internal sealed class Request
{
    public string mode { get; set; }
    public string projectRoot { get; set; }
    public List<AssemblyInput> assemblies { get; set; } = new List<AssemblyInput>();
    public RenameInput rename { get; set; }
}
internal sealed class AssemblyInput
{
    public string name { get; set; }
    public List<string> references { get; set; } = new List<string>();
    public List<string> projectReferences { get; set; } = new List<string>();
    public List<string> defines { get; set; } = new List<string>();
    public string languageVersion { get; set; } = "default";
    public string nullable { get; set; } = "disable";
    public bool allowUnsafe { get; set; }
    public List<SourceInput> files { get; set; } = new List<SourceInput>();
    public Dictionary<string, string> referenceHashes { get; set; } = new Dictionary<string, string>();
    public List<ContextFileInput> analyzers { get; set; } = new List<ContextFileInput>();
    public List<ContextFileInput> additionalFiles { get; set; } = new List<ContextFileInput>();
}
internal sealed class ContextFileInput
{
    public string path { get; set; }
    public string sha256 { get; set; }
}
internal sealed class SourceInput
{
    public string path { get; set; }
    public string absolutePath { get; set; }
    public string sha256 { get; set; }
}
internal sealed class RenameInput
{
    public string kind { get; set; }
    public string symbol { get; set; }
    public string containingType { get; set; }
    public string newName { get; set; }
    public string assembly { get; set; }
}
internal sealed class Patch
{
    public string sourcePath { get; set; }
    public string targetPath { get; set; }
    public string expectedSha256 { get; set; }
    public string afterSha256 { get; set; }
    public string content { get; set; }
    public string metaGuidSha256 { get; set; }
    public string metaSourcePath { get; set; }
    public string metaTargetPath { get; set; }
    public bool preserveMetaGuid { get; set; }
}
internal sealed class AssemblyBuild
{
    public AssemblyInput Input;
    public CSharpCompilation Compilation;
    public List<SyntaxTree> Trees = new List<SyntaxTree>();
    public List<Diagnostic> Diagnostics = new List<Diagnostic>();
    public List<object> AnalyzerDiagnostics = new List<object>();
    public List<object> GeneratorDiagnostics = new List<object>();
    public List<object> AnalysisFailures = new List<object>();
    public List<object> GeneratedSources = new List<object>();
    public List<string> LoadedAnalyzers = new List<string>();
    public List<string> LoadedGenerators = new List<string>();
    public byte[] Emitted;
}

internal sealed class LocalAnalyzerAssemblyLoader : IAnalyzerAssemblyLoader
{
    readonly HashSet<string> locations = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    public void AddDependencyLocation(string fullPath) => locations.Add(Path.GetFullPath(fullPath));
    public Assembly LoadFromPath(string fullPath)
    {
        string path = Path.GetFullPath(fullPath);
        if (!File.Exists(path)) throw new FileNotFoundException("Analyzer dependency is missing", path);
        return AssemblyLoadContext.Default.LoadFromAssemblyPath(path);
    }
}

internal sealed class FileAdditionalText : AdditionalText
{
    readonly string path;
    public FileAdditionalText(string path) => this.path = System.IO.Path.GetFullPath(path);
    public override string Path => path;
    public override SourceText GetText(System.Threading.CancellationToken cancellationToken = default)
        => SourceText.From(File.ReadAllText(path, Encoding.UTF8), Encoding.UTF8);
}

internal static class Program
{
    static readonly JsonSerializerOptions JsonOptions = new JsonSerializerOptions { PropertyNameCaseInsensitive = true, WriteIndented = true };

    static void Main(string[] args)
    {
        if (args.Length != 2) throw new ArgumentException("Usage: BeaverSemantic request.json response.json");
        var request = JsonSerializer.Deserialize<Request>(File.ReadAllText(args[0]), JsonOptions);
        if (request == null) throw new InvalidDataException("Request JSON is empty");
        var response = request.mode == "inspect" ? Inspect(request) : request.mode == "rename" ? Rename(request) :
            throw new InvalidDataException("Unsupported helper mode " + request.mode);
        File.WriteAllText(args[1], JsonSerializer.Serialize(response, JsonOptions) + "\n");
    }

    static object Inspect(Request request)
    {
        var builds = BuildAll(request, null);
        var diagnostics = builds.SelectMany(x => x.Diagnostics).Where(x => x.Severity == DiagnosticSeverity.Error).Select(DiagnosticRow).ToList();
        diagnostics.AddRange(builds.SelectMany(x => x.AnalysisFailures));
        return new
        {
            contract = "beaver-csharp-helper/1",
            status = diagnostics.Count == 0 ? "ok" : "blocked",
            assemblies = builds.Select(x => new
            {
                name = x.Input.name,
                sourceCount = x.Trees.Count,
                sourceFiles = x.Input.files.Select(f => f.path).OrderBy(f => f),
                sources = x.Input.files.Select(f => new { path = f.path, sha256 = f.sha256 }),
                references = x.Input.references,
                referenceHashes = x.Input.referenceHashes,
                projectReferences = x.Input.projectReferences,
                defines = x.Input.defines,
                languageVersion = x.Input.languageVersion,
                analyzerInputs = x.Input.analyzers,
                additionalFileInputs = x.Input.additionalFiles,
                analyzerDiagnostics = x.AnalyzerDiagnostics,
                generatorDiagnostics = x.GeneratorDiagnostics,
                analysisFailures = x.AnalysisFailures,
                loadedAnalyzers = x.LoadedAnalyzers,
                loadedGenerators = x.LoadedGenerators,
                generatedSources = x.GeneratedSources,
                errors = x.Diagnostics.Count(d => d.Severity == DiagnosticSeverity.Error),
                warnings = x.Diagnostics.Count(d => d.Severity == DiagnosticSeverity.Warning),
                declaredTypes = x.Compilation.Assembly.GlobalNamespace.GetNamespaceMembers().SelectMany(FlattenTypes).Select(t => new
                {
                    symbol = Display(t), kind = t.TypeKind.ToString(), partialParts = t.DeclaringSyntaxReferences.Length,
                    source = t.Locations.Any(l => l.IsInSource), unityObject = IsUnityObject(t)
                }).OrderBy(t => t.symbol)
            }).ToArray(),
            diagnostics
        };
    }

    static IEnumerable<INamedTypeSymbol> FlattenTypes(INamespaceSymbol ns)
    {
        foreach (var type in ns.GetTypeMembers()) yield return type;
        foreach (var child in ns.GetNamespaceMembers())
            foreach (var type in FlattenTypes(child)) yield return type;
    }

    static object Rename(Request request)
    {
        if (request.rename == null) throw new InvalidDataException("Rename details are required");
        var builds = BuildAll(request, null);
        var errors = builds.SelectMany(x => x.Diagnostics).Where(x => x.Severity == DiagnosticSeverity.Error).Select(DiagnosticRow).ToList();
        errors.AddRange(builds.SelectMany(x => x.AnalysisFailures));
        if (errors.Count != 0)
            return Failure("Baseline compilation contains errors; Roslyn symbol identity is not complete.", errors);
        var targetBuild = builds.SingleOrDefault(x => x.Input.name == request.rename.assembly);
        if (targetBuild == null) return Failure("Requested assembly is not present in the compile input.", new object[] { new { code = "ASSEMBLY_NOT_FOUND", assembly = request.rename.assembly } });
        ISymbol target = ResolveTarget(targetBuild.Compilation, request.rename);
        if (target == null) return Failure("Requested symbol is missing, ambiguous, or not source-backed.", new object[] { new { code = "SYMBOL_NOT_SOURCE_BACKED", symbol = request.rename.symbol, kind = request.rename.kind } });
        if (target.Locations.Any(l => !l.IsInSource))
            return Failure("A symbol reference resolves outside the supplied source set.", new object[] { new { code = "EXTERNAL_SYMBOL_REFERENCE", symbol = Display(target) } });
        if (request.rename.kind == "type" && target is INamedTypeSymbol movedType && IsUnityObject(movedType) &&
            HasMovedFrom(movedType) && !MovedFromAllowsMultiple(targetBuild.Compilation))
            return Failure("The type already carries a single-use MovedFrom identity; adding the current type as another migration source would break that identity.",
                new object[] { new { code = "MOVED_FROM_MIGRATION_CHAIN_UNSUPPORTED", symbol = Display(target),
                    existingMovedFrom = movedType.GetAttributes().Where(a => a.AttributeClass?.ToDisplayString() == "UnityEngine.Scripting.APIUpdating.MovedFromAttribute")
                        .Select(a => a.ToString()).ToArray(), allowMultiple = false } });
        if (request.rename.kind == "namespace" && target is INamespaceSymbol && !MovedFromAllowsMultiple(targetBuild.Compilation))
        {
            var affected = builds.SelectMany(build => FlattenTypes(build.Compilation.Assembly.GlobalNamespace))
                .Where(type => type.Locations.Any(location => location.IsInSource) && IsUnityObject(type) &&
                    NamespaceHasPrefix(type.ContainingNamespace, request.rename.symbol) && HasMovedFrom(type))
                .Select(type => new { symbol = Display(type), existingMovedFrom = type.GetAttributes()
                    .Where(a => a.AttributeClass?.ToDisplayString() == "UnityEngine.Scripting.APIUpdating.MovedFromAttribute")
                    .Select(a => a.ToString()).ToArray() }).ToArray();
            if (affected.Length != 0)
                return Failure("Namespace migration would add a second single-use MovedFrom identity to existing Unity types.",
                    affected.Select(type => (object)new { code = "MOVED_FROM_MIGRATION_CHAIN_UNSUPPORTED",
                        symbol = type.symbol, existingMovedFrom = type.existingMovedFrom, allowMultiple = false }));
        }
        if (request.rename.kind == "type" && target is INamedTypeSymbol generic && generic.Arity != 0)
            return Failure("Generic type renames are outside the supported subset.", new object[] { new { code = "GENERIC_TYPE_UNSUPPORTED", symbol = Display(target) } });
        if (request.rename.kind == "serialized_field" && !(target is IFieldSymbol serialized && IsSerializedField(serialized)))
            return Failure("The selected field is not a supported serialized field on a Unity object.", new object[] { new { code = "FIELD_NOT_SERIALIZED", symbol = Display(target) } });
        if (HasCollision(targetBuild.Compilation, target, request.rename))
            return Failure("The destination symbol already exists in the containing scope.", new object[] { new { code = "RENAME_COLLISION", symbol = request.rename.newName } });
        if (request.rename.kind == "serialized_field" && targetBuild.Compilation.GetTypeByMetadataName("UnityEngine.Serialization.FormerlySerializedAsAttribute") == null)
            return Failure("Unity FormerlySerializedAsAttribute is absent from the compilation references.", new object[] { new { code = "FORMERLY_SERIALIZED_AS_UNAVAILABLE" } });

        var targetKey = SymbolKey(target);
        var unknown = new List<object>();
        var patches = new List<Patch>();
        int boundReferenceCount = 0;
        int declarationCount = 0;
        bool partialTarget = target is INamedTypeSymbol named && named.DeclaringSyntaxReferences.Length > 1;
        foreach (var build in builds)
        {
            foreach (var tree in build.Trees)
            {
                var model = build.Compilation.GetSemanticModel(tree, ignoreAccessibility: false);
                string before = tree.GetText().ToString();
                var root = tree.GetRoot();
                var oldSimple = request.rename.kind == "serialized_field" ? request.rename.symbol : target.Name;
                foreach (var text in root.DescendantTrivia(descendIntoTrivia: true).Where(t => t.IsKind(SyntaxKind.DisabledTextTrivia)))
                {
                    var disabledTokens = CSharpSyntaxTree.ParseText(text.ToString(), tree.Options as CSharpParseOptions).GetRoot().DescendantTokens();
                    if (disabledTokens.Any(t => t.IsKind(SyntaxKind.IdentifierToken) && t.ValueText == oldSimple))
                        unknown.Add(new { code = "CONDITIONAL_DISABLED_REFERENCE", path = tree.FilePath, span = text.SpanStart, symbol = oldSimple });
                }
                var checkedConstantSpans = new HashSet<Microsoft.CodeAnalysis.Text.TextSpan>();
                foreach (var expression in root.DescendantNodes().OfType<ExpressionSyntax>())
                {
                    var constant = model.GetConstantValue(expression);
                    if (!constant.HasValue || constant.Value is not string value || !checkedConstantSpans.Add(expression.Span) ||
                        IsAssetAddressExpression(model, expression) || !ContainsSymbolText(value, request.rename, target)) continue;
                    unknown.Add(new { code = "UNKNOWN_STRING_REFERENCE", path = tree.FilePath, span = expression.SpanStart, value });
                }
                foreach (var textPart in root.DescendantNodes().OfType<InterpolatedStringTextSyntax>())
                    if (ContainsSymbolText(textPart.TextToken.ValueText, request.rename, target))
                        unknown.Add(new { code = "UNKNOWN_INTERPOLATED_STRING_REFERENCE", path = tree.FilePath, span = textPart.SpanStart, value = textPart.TextToken.ValueText });
                foreach (var invocation in root.DescendantNodes().OfType<InvocationExpressionSyntax>().Where(invocation => IsReflectionTypeLookup(model, invocation)))
                    foreach (var argument in invocation.ArgumentList.Arguments)
                    {
                        if (IsAssetAddressInvocation(model, invocation, argument)) continue;
                        var constant = model.GetConstantValue(argument.Expression);
                        if (constant.HasValue && constant.Value is string) continue;
                        if (IsClosedSafeStringParameter(model, argument.Expression, request.rename, target)) continue;
                        if (model.GetTypeInfo(argument.Expression).ConvertedType?.SpecialType == SpecialType.System_String)
                            unknown.Add(new { code = "UNKNOWN_DYNAMIC_REFLECTION_REFERENCE", path = tree.FilePath,
                                span = argument.Expression.SpanStart, api = model.GetSymbolInfo(invocation).Symbol?.ToDisplayString(),
                                expression = argument.Expression.ToString() });
                    }

                var rewriter = new BoundRenameRewriter(model, request.rename, targetKey, target);
                var changedRoot = rewriter.Visit(root);
                boundReferenceCount += rewriter.ReferenceCount;
                declarationCount += rewriter.DeclarationCount;
                if (rewriter.Unsupported.Count != 0) unknown.AddRange(rewriter.Unsupported);
                string after = changedRoot.ToFullString();
                if (after == before) continue;
                var source = request.projectRoot == null ? null : Path.GetFullPath(Path.IsPathRooted(tree.FilePath) ? tree.FilePath : Path.Combine(request.projectRoot, tree.FilePath));
                string relative = Path.GetRelativePath(request.projectRoot, source).Replace('\\', '/');
                string targetPath = relative;
                string metaHash = null;
                bool preserveMeta = false;
                if (request.rename.kind == "type" && target is INamedTypeSymbol type && IsUnityObject(type) &&
                    Path.GetFileNameWithoutExtension(relative) == target.Name)
                {
                    targetPath = Path.Combine(Path.GetDirectoryName(relative) ?? "", request.rename.newName + ".cs").Replace('\\', '/');
                    string metaPath = source + ".meta";
                    if (!File.Exists(metaPath))
                        unknown.Add(new { code = "MONOSCRIPT_META_MISSING", path = relative, message = "Unity script GUID cannot be preserved without the adjacent .meta." });
                    else
                    {
                        metaHash = HashFile(metaPath);
                        preserveMeta = true;
                    }
                }
                patches.Add(new Patch { sourcePath = relative, targetPath = targetPath, expectedSha256 = tree.FilePath == null ? HashText(before) : FindSourceHash(request, relative),
                    afterSha256 = HashText(after), content = after, metaGuidSha256 = metaHash,
                    metaSourcePath = preserveMeta ? relative + ".meta" : null,
                    metaTargetPath = preserveMeta ? targetPath + ".meta" : null, preserveMetaGuid = preserveMeta });
            }
            var generatedTrees = build.Compilation.SyntaxTrees.Where(tree => !build.Trees.Contains(tree));
            foreach (var tree in generatedTrees)
            {
                var root = tree.GetRoot();
                string oldSimple = request.rename.kind == "serialized_field" ? request.rename.symbol : target.Name;
                foreach (var literal in root.DescendantNodes().OfType<LiteralExpressionSyntax>().Where(n => n.IsKind(SyntaxKind.StringLiteralExpression)))
                    if (ContainsSymbolText(literal.Token.ValueText, request.rename, target))
                        unknown.Add(new { code = "UNKNOWN_GENERATED_STRING_REFERENCE", path = tree.FilePath,
                            span = literal.SpanStart, value = literal.Token.ValueText });
                foreach (var token in root.DescendantTokens().Where(t => t.IsKind(SyntaxKind.IdentifierToken) && t.ValueText == oldSimple))
                    if (token.Parent is null || build.Compilation.GetSemanticModel(tree).GetSymbolInfo(token.Parent).Symbol is not ISymbol)
                        unknown.Add(new { code = "UNKNOWN_GENERATED_IDENTIFIER_REFERENCE", path = tree.FilePath,
                            span = token.SpanStart, value = token.ValueText });
            }
        }
        if (unknown.Count != 0)
            return Failure("Unknown, inactive-conditional, or serialized migration references prevent an automatic candidate.", unknown);
        if (declarationCount == 0 || boundReferenceCount == 0 && request.rename.kind != "namespace")
            return Failure("Roslyn did not bind a complete declaration/reference set for this rename.", new object[] { new { code = "SYMBOL_BINDING_INCOMPLETE", declarations = declarationCount, references = boundReferenceCount } });
        var emittedRequest = CloneWithPatches(request, builds, patches);
        var afterBuilds = BuildAll(emittedRequest, patches);
        var afterErrors = afterBuilds.SelectMany(x => x.Diagnostics).Where(x => x.Severity == DiagnosticSeverity.Error).Select(DiagnosticRow).ToList();
        afterErrors.AddRange(afterBuilds.SelectMany(x => x.AnalysisFailures));
        if (afterErrors.Count != 0)
        {
            if (request.rename.kind == "type")
            {
                var afterTargetAssembly = afterBuilds.SingleOrDefault(x => x.Input.name == request.rename.assembly);
                string requestedType = request.rename.symbol.TrimStart("global::".ToCharArray());
                int separator = requestedType.LastIndexOf('.');
                string candidateType = (separator < 0 ? "" : requestedType.Substring(0, separator + 1)) + request.rename.newName;
                var declaration = afterTargetAssembly?.Compilation.GetTypeByMetadataName(candidateType);
                afterErrors.Add(new { code = "CANDIDATE_TYPE_BINDING_SUMMARY", assembly = request.rename.assembly,
                    candidateType, candidateDeclarationFound = declaration != null,
                    candidateDeclarationIsSourceBacked = declaration?.Locations.Any(location => location.IsInSource) ?? false,
                    assemblyBindings = afterBuilds.Select(build => new { name = build.Input.name,
                        resolvesCandidate = build.Compilation.GetTypeByMetadataName(candidateType) != null,
                        hasEmittedImage = build.Emitted != null, projectReferences = build.Input.projectReferences }).ToArray(),
                    editedSources = patches.Select(p => new { path = p.sourcePath, targetPath = p.targetPath,
                        sha256 = p.afterSha256, length = p.content.Length,
                        qualifiedCandidateReferences = CountText(p.content, request.rename.symbol.Substring(0, Math.Max(0, request.rename.symbol.LastIndexOf('.') + 1)) + request.rename.newName),
                        simpleCandidateReferences = CountText(p.content, request.rename.newName),
                        candidateLines = p.content.Split('\n').Where(line => line.Contains(request.rename.newName, StringComparison.Ordinal)).Take(4).ToArray() }).ToArray() });
            }
            return Failure("Candidate compilation introduced or retained compiler errors.", afterErrors);
        }
        return new
        {
            contract = "beaver-csharp-helper/1",
            status = "ok",
            kind = request.rename.kind,
            symbol = Display(target),
            assembly = targetBuild.Input.name,
            declarationCount,
            boundReferenceCount,
            partialTypeSupported = partialTarget,
            inheritanceReferences = "Roslyn-bound type references, including base lists, are rewritten when present in supplied source assemblies.",
            aliasReferences = "Roslyn-bound alias targets are rewritten; alias identifiers are retained.",
            reflection = "String-valued dynamic references are not rewritten; matching string literals block the plan.",
            defines = builds.Select(x => new { assembly = x.Input.name, symbols = x.Input.defines }).ToArray(),
            assemblies = builds.Select(x => new { name = x.Input.name,
                sources = x.Input.files.Select(f => new { path = f.path, sha256 = f.sha256 }),
                references = x.Input.references, referenceHashes = x.Input.referenceHashes,
                projectReferences = x.Input.projectReferences,
                analyzerInputs = x.Input.analyzers, additionalFileInputs = x.Input.additionalFiles,
                analyzerDiagnostics = x.AnalyzerDiagnostics, generatorDiagnostics = x.GeneratorDiagnostics,
                analysisFailures = x.AnalysisFailures, loadedAnalyzers = x.LoadedAnalyzers,
                loadedGenerators = x.LoadedGenerators, generatedSources = x.GeneratedSources }).ToArray(),
            candidateAssemblies = afterBuilds.Select(x => new { name = x.Input.name,
                analyzerDiagnostics = x.AnalyzerDiagnostics, generatorDiagnostics = x.GeneratorDiagnostics,
                analysisFailures = x.AnalysisFailures, generatedSources = x.GeneratedSources }).ToArray(),
            baselineDiagnostics = builds.SelectMany(x => x.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Warning)).Select(DiagnosticRow).ToArray(),
            candidateErrors = afterErrors,
            patches,
            unknownReferences = unknown
        };
    }

    static object Failure(string message, IEnumerable<object> diagnostics) => new
    {
        contract = "beaver-csharp-helper/1", status = "blocked", message, patches = Array.Empty<Patch>(),
        diagnostics = diagnostics.ToArray(), unknownReferences = diagnostics.ToArray()
    };

    static Request CloneWithPatches(Request request, List<AssemblyBuild> builds, List<Patch> patches)
    {
        var clone = JsonSerializer.Deserialize<Request>(JsonSerializer.Serialize(request, JsonOptions), JsonOptions);
        var map = patches.ToDictionary(p => p.sourcePath, p => p.content, StringComparer.Ordinal);
        foreach (var assembly in clone.assemblies)
            foreach (var file in assembly.files)
                if (map.TryGetValue(file.path, out string text))
                {
                    string from = Path.GetFullPath(Path.Combine(request.projectRoot, file.path));
                    file.absolutePath = from;
                    file.sha256 = HashText(text);
                    // Reuse the original logical path; BuildAll reads overlay content below.
                }
        // The helper reuses an explicit overlay for verification rather than writing candidate sources.
        _overlays = map;
        return clone;
    }
    static Dictionary<string, string> _overlays;

    static List<AssemblyBuild> BuildAll(Request request, List<Patch> overlays)
    {
        if (overlays != null) _overlays = overlays.ToDictionary(p => p.sourcePath, p => p.content, StringComparer.Ordinal);
        else if (_overlays == null) _overlays = new Dictionary<string, string>();
        var specs = request.assemblies.ToDictionary(x => x.name, StringComparer.Ordinal);
        var ordered = new List<AssemblyInput>();
        var visiting = new HashSet<string>(); var visited = new HashSet<string>();
        Action<AssemblyInput> visit = null;
        visit = item =>
        {
            if (visited.Contains(item.name)) return;
            if (!visiting.Add(item.name)) throw new InvalidDataException("Compilation project reference cycle at " + item.name);
            foreach (var dep in item.projectReferences ?? new List<string>())
                if (specs.TryGetValue(dep, out var dependency)) visit(dependency);
                else throw new InvalidDataException("Project reference is not supplied: " + dep);
            visiting.Remove(item.name); visited.Add(item.name); ordered.Add(item);
        };
        foreach (var item in request.assemblies) visit(item);
        var result = new List<AssemblyBuild>();
        var emitted = new Dictionary<string, byte[]>(StringComparer.Ordinal);
        foreach (var item in ordered)
        {
            var references = new List<MetadataReference>();
            foreach (var path in item.references.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path)) throw new FileNotFoundException("Metadata reference missing", path);
                references.Add(MetadataReference.CreateFromFile(path));
            }
            foreach (var dependency in item.projectReferences ?? new List<string>())
            {
                if (emitted.TryGetValue(dependency, out var image)) references.Add(MetadataReference.CreateFromImage(image));
            }
            if (!LanguageVersionFacts.TryParse(item.languageVersion ?? "default", out var language))
                throw new InvalidDataException("Unsupported C# languageVersion " + item.languageVersion);
            var parse = new CSharpParseOptions(language, preprocessorSymbols: item.defines ?? new List<string>());
            var trees = new List<SyntaxTree>();
            foreach (var source in item.files)
            {
                string text;
                if (_overlays.TryGetValue(source.path, out var overlay)) text = overlay;
                else
                {
                    if (HashFile(source.absolutePath) != source.sha256)
                        throw new InvalidDataException("Compile input changed after inventory: " + source.path);
                    text = File.ReadAllText(source.absolutePath);
                }
                trees.Add(CSharpSyntaxTree.ParseText(text, parse, source.path));
            }
            var nullable = (item.nullable ?? "disable").ToLowerInvariant() switch
            {
                "enable" => NullableContextOptions.Enable,
                "annotations" => NullableContextOptions.Annotations,
                "warnings" => NullableContextOptions.Warnings,
                _ => NullableContextOptions.Disable
            };
            var compilation = CSharpCompilation.Create(item.name, trees, references,
                new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary, allowUnsafe: item.allowUnsafe, nullableContextOptions: nullable));
            var build = new AssemblyBuild { Input = item, Compilation = compilation, Trees = trees };
            foreach (var input in item.analyzers ?? new List<ContextFileInput>())
                if (!File.Exists(input.path) || HashFile(input.path) != input.sha256)
                    throw new InvalidDataException("Analyzer input changed after context inventory: " + input.path);
            foreach (var input in item.additionalFiles ?? new List<ContextFileInput>())
                if (!File.Exists(input.path) || HashFile(input.path) != input.sha256)
                    throw new InvalidDataException("Additional file changed after context inventory: " + input.path);
            var additionalTexts = (item.additionalFiles ?? new List<ContextFileInput>())
                .Select(x => (AdditionalText)new FileAdditionalText(x.path)).ToList();
            if ((item.analyzers?.Count ?? 0) != 0)
            {
                var loader = new LocalAnalyzerAssemblyLoader();
                foreach (var analyzer in item.analyzers) loader.AddDependencyLocation(analyzer.path);
                var analyzerLoadFailures = new List<object>();
                var analyzerReferences = new List<AnalyzerFileReference>();
                foreach (var analyzer in item.analyzers)
                {
                    var reference = new AnalyzerFileReference(analyzer.path, loader);
                    reference.AnalyzerLoadFailed += (_, args) => analyzerLoadFailures.Add(new {
                        code = "ANALYZER_LOAD_FAILED", path = analyzer.path, message = args.Message, typeName = args.TypeName });
                    analyzerReferences.Add(reference);
                }
                var generators = analyzerReferences.SelectMany(x => x.GetGenerators(LanguageNames.CSharp)).ToList();
                build.LoadedGenerators.AddRange(generators.Select(x => x.GetType().FullName).Where(x => x != null));
                if (generators.Count != 0)
                {
                    GeneratorDriver driver = CSharpGeneratorDriver.Create(generators, additionalTexts, parse, null);
                    driver = driver.RunGeneratorsAndUpdateCompilation(compilation, out var generatedCompilation, out var generatorDiagnostics);
                    compilation = (CSharpCompilation)generatedCompilation;
                    build.GeneratorDiagnostics.AddRange(generatorDiagnostics.Select(DiagnosticRow));
                    build.Diagnostics.AddRange(generatorDiagnostics);
                    foreach (var diagnostic in generatorDiagnostics.Where(d => d.Id == "CS8784" || d.Id == "CS8785"))
                        build.AnalysisFailures.Add(new { code = "SOURCE_GENERATOR_FAILED", id = diagnostic.Id,
                            message = diagnostic.GetMessage(), path = diagnostic.Location.IsInSource ? diagnostic.Location.SourceTree.FilePath : null });
                    var run = driver.GetRunResult();
                    foreach (var generatorResult in run.Results)
                        foreach (var generated in generatorResult.GeneratedSources)
                        {
                            string generatedText = generated.SourceText.ToString();
                            build.GeneratedSources.Add(new { generator = generatorResult.Generator.GetType().FullName,
                                hintName = generated.HintName, sha256 = HashText(generatedText), textLength = generatedText.Length });
                        }
                }
                var analyzers = analyzerReferences.SelectMany(x => x.GetAnalyzers(LanguageNames.CSharp)).ToList();
                build.LoadedAnalyzers.AddRange(analyzers.Select(x => x.GetType().FullName).Where(x => x != null));
                build.AnalysisFailures.AddRange(analyzerLoadFailures);
                if (analyzers.Count != 0)
                {
                    var analyzerDiagnostics = compilation.WithAnalyzers(System.Collections.Immutable.ImmutableArray.CreateRange(analyzers))
                        .GetAnalyzerDiagnosticsAsync().GetAwaiter().GetResult();
                    build.AnalyzerDiagnostics.AddRange(analyzerDiagnostics.Select(DiagnosticRow));
                    build.Diagnostics.AddRange(analyzerDiagnostics);
                    foreach (var diagnostic in analyzerDiagnostics.Where(d => d.Id == "AD0001"))
                        build.AnalysisFailures.Add(new { code = "DIAGNOSTIC_ANALYZER_FAILED", id = diagnostic.Id,
                            message = diagnostic.GetMessage(), path = diagnostic.Location.IsInSource ? diagnostic.Location.SourceTree.FilePath : null });
                }
            }
            build.Compilation = compilation;
            build.Diagnostics.AddRange(compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error || d.Severity == DiagnosticSeverity.Warning));
            build.Diagnostics = build.Diagnostics.Distinct().ToList();
            using var stream = new MemoryStream();
            var emit = compilation.Emit(stream);
            if (emit.Success) { build.Emitted = stream.ToArray(); emitted[item.name] = build.Emitted; }
            result.Add(build);
        }
        return result;
    }

    static string FindSourceHash(Request request, string relativePath)
    {
        string normalized = relativePath.Replace('\\', '/');
        return request.assemblies.SelectMany(a => a.files).First(f => f.path.Replace('\\', '/') == normalized).sha256;
    }

    static ISymbol ResolveTarget(CSharpCompilation compilation, RenameInput rename)
    {
        ISymbol target = null;
        if (rename.kind == "type") target = compilation.GetTypeByMetadataName(rename.symbol.TrimStart("global::".ToCharArray()));
        else if (rename.kind == "namespace") target = FindNamespace(compilation.Assembly.GlobalNamespace, rename.symbol.Split('.'));
        else if (rename.kind == "serialized_field")
        {
            var type = compilation.GetTypeByMetadataName(rename.containingType.TrimStart("global::".ToCharArray()));
            var members = type?.GetMembers(rename.symbol).OfType<IFieldSymbol>().ToArray() ?? Array.Empty<IFieldSymbol>();
            if (members.Length == 1) target = members[0];
        }
        return target != null && target.Locations.Any(l => l.IsInSource) ? target : null;
    }

    static INamespaceSymbol FindNamespace(INamespaceSymbol current, string[] parts, int index = 0)
    {
        if (index == parts.Length) return current;
        var matches = current.GetNamespaceMembers().Where(x => x.Name == parts[index]).ToArray();
        return matches.Length == 1 ? FindNamespace(matches[0], parts, index + 1) : null;
    }

    static bool HasCollision(CSharpCompilation compilation, ISymbol target, RenameInput rename)
    {
        if (rename.kind == "type" && target is INamedTypeSymbol type)
        {
            string ns = type.ContainingNamespace.IsGlobalNamespace ? "" : type.ContainingNamespace.ToDisplayString();
            string metadataName = (ns.Length == 0 ? "" : ns + ".") + rename.newName;
            return compilation.GetTypeByMetadataName(metadataName) != null;
        }
        if (rename.kind == "serialized_field" && target is IFieldSymbol field)
            return field.ContainingType.GetMembers(rename.newName).Length != 0;
        if (rename.kind == "namespace" && target is INamespaceSymbol nsym)
        {
            var parent = nsym.ContainingNamespace;
            return parent.GetNamespaceMembers().Any(x => x.Name == rename.newName);
        }
        return false;
    }

    static bool IsSerializedField(IFieldSymbol field)
    {
        if (field.IsStatic || field.IsConst || field.IsReadOnly || !IsUnityObject(field.ContainingType)) return false;
        return field.DeclaredAccessibility == Accessibility.Public || field.GetAttributes().Any(a =>
            a.AttributeClass?.ToDisplayString() == "UnityEngine.SerializeField" ||
            a.AttributeClass?.ToDisplayString() == "UnityEngine.SerializeReference");
    }

    static bool IsUnityObject(INamedTypeSymbol type)
    {
        for (var current = type; current != null; current = current.BaseType)
        {
            var name = current.ToDisplayString();
            if (name == "UnityEngine.MonoBehaviour" || name == "UnityEngine.ScriptableObject") return true;
        }
        return false;
    }

    static bool HasMovedFrom(INamedTypeSymbol type)
        => type.GetAttributes().Any(a => a.AttributeClass?.ToDisplayString() == "UnityEngine.Scripting.APIUpdating.MovedFromAttribute");

    static bool MovedFromAllowsMultiple(CSharpCompilation compilation)
    {
        var movedFrom = compilation.GetTypeByMetadataName("UnityEngine.Scripting.APIUpdating.MovedFromAttribute");
        var usageAttribute = compilation.GetTypeByMetadataName("System.AttributeUsageAttribute");
        if (movedFrom == null || usageAttribute == null) return false;
        var usage = movedFrom.GetAttributes().FirstOrDefault(a => SymbolEqualityComparer.Default.Equals(a.AttributeClass, usageAttribute));
        return usage != null && usage.NamedArguments.Any(pair => pair.Key == "AllowMultiple" && pair.Value.Value is bool value && value);
    }

    static string Display(ISymbol symbol) => symbol.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat).Replace("global::", "");
    static string SymbolKey(ISymbol symbol)
    {
        if (symbol is INamespaceSymbol ns) return "N:" + Display(ns) + "@" + ns.ContainingAssembly?.Identity.Name;
        if (symbol is IFieldSymbol field) return "F:" + Display(field.ContainingType) + "." + field.Name + "@" + field.ContainingAssembly.Identity.Name;
        return "T:" + Display(symbol) + "@" + symbol.ContainingAssembly?.Identity.Name;
    }
    static bool Same(ISymbol symbol, string key) => symbol != null && SymbolKey(symbol) == key;

    static bool IsAssetAddressExpression(SemanticModel model, ExpressionSyntax expression)
    {
        var argument = expression.AncestorsAndSelf().OfType<ArgumentSyntax>().FirstOrDefault();
        var invocation = argument?.Parent?.Parent as InvocationExpressionSyntax;
        return invocation != null && IsAssetAddressInvocation(model, invocation, argument);
    }

    static bool IsAssetAddressInvocation(SemanticModel model, InvocationExpressionSyntax invocation, ArgumentSyntax argument)
    {
        var method = model.GetSymbolInfo(invocation).Symbol as IMethodSymbol;
        if (method == null || invocation.ArgumentList.Arguments.Count == 0 ||
            invocation.ArgumentList.Arguments[0].Span != argument.Span) return false;
        string type = method.ContainingType?.ToDisplayString();
        return type == "UnityEngine.Resources" && method.Name == "Load" ||
            type == "UnityEngine.SceneManagement.SceneManager" && (method.Name == "LoadScene" || method.Name == "LoadSceneAsync") ||
            type == "UnityEngine.AddressableAssets.Addressables" && method.Name == "LoadAssetAsync";
    }

    static bool IsReflectionTypeLookup(SemanticModel model, InvocationExpressionSyntax invocation)
    {
        var method = model.GetSymbolInfo(invocation).Symbol as IMethodSymbol;
        if (method == null) return false;
        string type = method.ContainingType?.ToDisplayString();
        return type == "System.Type" && method.Name == "GetType" ||
            type == "System.Reflection.Assembly" && method.Name == "GetType" ||
            type == "System.Activator" && method.Name == "CreateInstance";
    }

    // Resolve the narrow case where reflection receives a method parameter and every
    // source callsite supplies a compile-time string that cannot name the symbol being
    // changed. This keeps generic reflection dynamic while proving constant wrappers
    // such as FindEditorType("UnityEditor.GameView") safe.
    static bool IsClosedSafeStringParameter(SemanticModel model, ExpressionSyntax expression, RenameInput rename, ISymbol target)
    {
        if (expression is not IdentifierNameSyntax identifier ||
            model.GetSymbolInfo(identifier).Symbol is not IParameterSymbol parameter) return false;
        var method = parameter.ContainingSymbol as IMethodSymbol;
        if (method == null) return false;
        int calls = 0;
        foreach (var tree in model.Compilation.SyntaxTrees)
        {
            var callerModel = model.Compilation.GetSemanticModel(tree);
            foreach (var invocation in tree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>())
            {
                if (!SymbolEqualityComparer.Default.Equals(callerModel.GetSymbolInfo(invocation).Symbol, method)) continue;
                int ordinal = parameter.Ordinal;
                if (invocation.ArgumentList.Arguments.Count <= ordinal) return false;
                var supplied = invocation.ArgumentList.Arguments[ordinal].Expression;
                var constant = callerModel.GetConstantValue(supplied);
                if (!constant.HasValue || constant.Value is not string value || ContainsSymbolText(value, rename, target)) return false;
                calls++;
            }
        }
        return calls > 0;
    }

    static bool ContainsSymbolText(string text, RenameInput rename, ISymbol target)
    {
        if (string.IsNullOrEmpty(text)) return false;
        var full = Display(target);
        var simple = rename.kind == "serialized_field" ? rename.symbol : target.Name;
        if (rename.kind == "namespace")
            return text == rename.symbol || text == "global::" + rename.symbol || text.StartsWith(rename.symbol + ".", StringComparison.Ordinal);
        if (text == full || text == "global::" + full || text == simple) return true;
        return false;
    }

    static string HashText(string text) => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    static int CountText(string source, string value)
    {
        if (string.IsNullOrEmpty(value)) return 0;
        int count = 0, index = 0;
        while ((index = source.IndexOf(value, index, StringComparison.Ordinal)) >= 0) { count++; index += value.Length; }
        return count;
    }
    static string HashFile(string path) => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant();
    static object DiagnosticRow(Diagnostic d) => new
    {
        code = d.Id, severity = d.Severity.ToString(), message = d.GetMessage(),
        path = d.Location.IsInSource ? d.Location.SourceTree.FilePath : null,
        line = d.Location.IsInSource ? d.Location.GetLineSpan().StartLinePosition.Line + 1 : 0,
        column = d.Location.IsInSource ? d.Location.GetLineSpan().StartLinePosition.Character + 1 : 0
    };

    static bool NamespaceHasPrefix(INamespaceSymbol symbol, string prefix)
    {
        string value = symbol.IsGlobalNamespace ? "" : symbol.ToDisplayString();
        return value == prefix || value.StartsWith(prefix + ".", StringComparison.Ordinal);
    }

    sealed class BoundRenameRewriter : CSharpSyntaxRewriter
    {
        readonly SemanticModel model; readonly RenameInput rename; readonly string key; readonly ISymbol target;
        readonly string assembly;
        public int ReferenceCount; public int DeclarationCount;
        public readonly List<object> Unsupported = new List<object>();
        public BoundRenameRewriter(SemanticModel model, RenameInput rename, string key, ISymbol target)
        { this.model = model; this.rename = rename; this.key = key; this.target = target; assembly = target.ContainingAssembly?.Identity.Name ?? rename.assembly; }
        bool IsTarget(SyntaxNode node)
        {
            var symbol = model.GetSymbolInfo(node).Symbol;
            if (symbol == null && node is QualifiedNameSyntax qualified) symbol = model.GetSymbolInfo(qualified.Right).Symbol;
            if (symbol == null && node is AliasQualifiedNameSyntax aliasQualified) symbol = model.GetSymbolInfo(aliasQualified.Name).Symbol;
            if (symbol == null && node is IdentifierNameSyntax identifier) symbol = model.GetAliasInfo(identifier)?.Target;
            if (symbol == null && node is GenericNameSyntax generic) symbol = model.GetAliasInfo(generic)?.Target;
            return Same(symbol, key);
        }
        SyntaxToken RenameToken(SyntaxToken token)
        {
            ReferenceCount++;
            return SyntaxFactory.Identifier(token.LeadingTrivia, rename.newName, token.TrailingTrivia);
        }
        public override SyntaxNode VisitIdentifierName(IdentifierNameSyntax node)
        {
            // `var` binds to the inferred type semantically, but remains a contextual
            // keyword and must never be rewritten as a type identifier.
            if (rename.kind == "type" && node.Identifier.ValueText == "var") return base.VisitIdentifierName(node);
            if (rename.kind == "namespace")
            {
                var ns = model.GetSymbolInfo(node).Symbol as INamespaceSymbol;
                if (ns != null && Same(ns, key) && node.Parent is NamespaceDeclarationSyntax declaration && declaration.Name == node)
                    return SyntaxFactory.ParseName(rename.newName).WithTriviaFrom(node);
            }
            return IsTarget(node) ? node.WithIdentifier(RenameToken(node.Identifier)) : base.VisitIdentifierName(node);
        }
        public override SyntaxNode VisitGenericName(GenericNameSyntax node)
            => IsTarget(node) ? node.WithIdentifier(RenameToken(node.Identifier)) : base.VisitGenericName(node);
        public override SyntaxNode VisitQualifiedName(QualifiedNameSyntax node)
        {
            if (rename.kind == "namespace")
            {
                var full = model.GetSymbolInfo(node).Symbol as INamespaceSymbol;
                if (full != null && Same(full, key)) return NamespaceReference(rename.newName).WithTriviaFrom(node);
                var left = model.GetSymbolInfo(node.Left).Symbol as INamespaceSymbol;
                if (left != null && Same(left, key)) return node.WithLeft(NamespaceReference(rename.newName).WithTriviaFrom(node.Left)).WithRight((SimpleNameSyntax)Visit(node.Right));
            }
            // A fully-qualified type reference must retain its left-hand qualifier.  Rewriting
            // the bound type's identifier in place avoids turning
            // `global::A.B.OldType` into an unqualified `NewType`.
            if (rename.kind == "type" && IsTarget(node))
            {
                if (node.Right is IdentifierNameSyntax rightIdentifier)
                    return node.WithRight(rightIdentifier.WithIdentifier(RenameToken(rightIdentifier.Identifier)));
                if (node.Right is GenericNameSyntax rightGeneric)
                    return node.WithRight(rightGeneric.WithIdentifier(RenameToken(rightGeneric.Identifier)));
            }
            return base.VisitQualifiedName(node);
        }
        public override SyntaxNode VisitAliasQualifiedName(AliasQualifiedNameSyntax node)
        {
            if (rename.kind == "namespace")
            {
                var ns = model.GetSymbolInfo(node).Symbol as INamespaceSymbol;
                if (ns != null && Same(ns, key)) return NamespaceReference(rename.newName).WithTriviaFrom(node);
            }
            if (rename.kind == "type" && IsTarget(node) && node.Name is IdentifierNameSyntax rightIdentifier)
                return node.WithName(rightIdentifier.WithIdentifier(RenameToken(rightIdentifier.Identifier)));
            return base.VisitAliasQualifiedName(node);
        }
        public override SyntaxNode VisitUsingDirective(UsingDirectiveSyntax node)
        {
            if (rename.kind == "namespace" && node.Name != null && IsTarget(node.Name))
            {
                ReferenceCount++;
                return node.WithName(NamespaceReference(rename.newName).WithTriviaFrom(node.Name));
            }
            return base.VisitUsingDirective(node);
        }
        public override SyntaxNode VisitNamespaceDeclaration(NamespaceDeclarationSyntax node)
        {
            var declared = model.GetDeclaredSymbol(node) as INamespaceSymbol;
            if (rename.kind == "namespace" && declared != null && NamespaceHasPrefix(declared, rename.symbol))
            {
                var enclosing = node.Ancestors().OfType<BaseNamespaceDeclarationSyntax>().FirstOrDefault();
                var enclosingSymbol = enclosing == null ? null : model.GetDeclaredSymbol(enclosing) as INamespaceSymbol;
                if (enclosingSymbol != null && NamespaceHasPrefix(enclosingSymbol, rename.symbol))
                    return base.VisitNamespaceDeclaration(node);
                if (enclosingSymbol != null)
                {
                    Unsupported.Add(new { code = "NESTED_NAMESPACE_MOVE_UNSUPPORTED", path = node.SyntaxTree.FilePath, span = node.SpanStart,
                        message = "A matching namespace declaration is nested under a namespace outside the move target." });
                    return base.VisitNamespaceDeclaration(node);
                }
                DeclarationCount++;
                var replacement = NamespaceReplacement(declared, node.Name);
                return node.WithName(replacement).WithExterns(VisitList(node.Externs)).WithUsings(VisitList(node.Usings)).WithMembers(VisitList(node.Members));
            }
            return base.VisitNamespaceDeclaration(node);
        }
        public override SyntaxNode VisitFileScopedNamespaceDeclaration(FileScopedNamespaceDeclarationSyntax node)
        {
            var declared = model.GetDeclaredSymbol(node) as INamespaceSymbol;
            if (rename.kind == "namespace" && declared != null && NamespaceHasPrefix(declared, rename.symbol))
            {
                DeclarationCount++;
                var replacement = NamespaceReplacement(declared, node.Name);
                return node.WithName(replacement).WithExterns(VisitList(node.Externs)).WithUsings(VisitList(node.Usings));
            }
            return base.VisitFileScopedNamespaceDeclaration(node);
        }
        NameSyntax NamespaceReplacement(INamespaceSymbol declared, NameSyntax original)
        {
            string oldName = declared.ToDisplayString();
            string suffix = oldName.Length == rename.symbol.Length ? "" : oldName.Substring(rename.symbol.Length);
            return SyntaxFactory.ParseName(rename.newName + suffix).WithTriviaFrom(original);
        }
        NameSyntax NamespaceReference(string name) => SyntaxFactory.ParseName("global::" + name);
        public override SyntaxNode VisitClassDeclaration(ClassDeclarationSyntax node)
        {
            var visited = (ClassDeclarationSyntax)base.VisitClassDeclaration(node);
            var symbol = model.GetDeclaredSymbol(node) as INamedTypeSymbol;
            if (rename.kind == "type" && Same(symbol, key))
            {
                DeclarationCount++;
                visited = visited.WithIdentifier(DeclarationToken(node.Identifier));
                return IsFirstPart(symbol, node) ? AddMovedFrom(visited, symbol, node.Identifier.ValueText) : visited;
            }
            if (rename.kind == "namespace" && symbol != null && IsUnityObject(symbol) && NamespaceHasPrefix(symbol.ContainingNamespace, rename.symbol) && IsFirstPart(symbol, node))
                return AddMovedFrom(visited, symbol, symbol.Name);
            return visited;
        }
        public override SyntaxNode VisitConstructorDeclaration(ConstructorDeclarationSyntax node)
        {
            var symbol = model.GetDeclaredSymbol(node) as IMethodSymbol;
            if (rename.kind != "type" || symbol?.MethodKind != MethodKind.Constructor || !Same(symbol.ContainingType, key))
                return base.VisitConstructorDeclaration(node);
            ReferenceCount++;
            return node.WithIdentifier(RenameToken(node.Identifier));
        }
        public override SyntaxNode VisitDestructorDeclaration(DestructorDeclarationSyntax node)
        {
            var symbol = model.GetDeclaredSymbol(node) as IMethodSymbol;
            if (rename.kind != "type" || symbol?.MethodKind != MethodKind.Destructor || !Same(symbol.ContainingType, key))
                return base.VisitDestructorDeclaration(node);
            ReferenceCount++;
            return node.WithIdentifier(RenameToken(node.Identifier));
        }
        public override SyntaxNode VisitStructDeclaration(StructDeclarationSyntax node)
        {
            var visited = (StructDeclarationSyntax)base.VisitStructDeclaration(node);
            if (rename.kind != "type" || !Same(model.GetDeclaredSymbol(node), key)) return visited;
            DeclarationCount++; return visited.WithIdentifier(DeclarationToken(node.Identifier));
        }
        public override SyntaxNode VisitInterfaceDeclaration(InterfaceDeclarationSyntax node)
        {
            var visited = (InterfaceDeclarationSyntax)base.VisitInterfaceDeclaration(node);
            if (rename.kind != "type" || !Same(model.GetDeclaredSymbol(node), key)) return visited;
            DeclarationCount++; return visited.WithIdentifier(DeclarationToken(node.Identifier));
        }
        public override SyntaxNode VisitEnumDeclaration(EnumDeclarationSyntax node)
        {
            var visited = (EnumDeclarationSyntax)base.VisitEnumDeclaration(node);
            if (rename.kind != "type" || !Same(model.GetDeclaredSymbol(node), key)) return visited;
            DeclarationCount++; return visited.WithIdentifier(DeclarationToken(node.Identifier));
        }
        public override SyntaxNode VisitRecordDeclaration(RecordDeclarationSyntax node)
        {
            var visited = (RecordDeclarationSyntax)base.VisitRecordDeclaration(node);
            var symbol = model.GetDeclaredSymbol(node) as INamedTypeSymbol;
            if (rename.kind == "type" && Same(symbol, key))
            {
                DeclarationCount++;
                visited = visited.WithIdentifier(DeclarationToken(node.Identifier));
                return IsFirstPart(symbol, node) ? AddMovedFrom(visited, symbol, node.Identifier.ValueText) : visited;
            }
            if (rename.kind == "namespace" && symbol != null && IsUnityObject(symbol) && NamespaceHasPrefix(symbol.ContainingNamespace, rename.symbol) && IsFirstPart(symbol, node))
                return AddMovedFrom(visited, symbol, symbol.Name);
            return visited;
        }
        static bool IsFirstPart(INamedTypeSymbol type, SyntaxNode node) => type.DeclaringSyntaxReferences
            .OrderBy(r => r.SyntaxTree.FilePath, StringComparer.Ordinal).ThenBy(r => r.Span.Start)
            .FirstOrDefault()?.Span.Start == node.SpanStart;
        public override SyntaxNode VisitDelegateDeclaration(DelegateDeclarationSyntax node)
        {
            if (rename.kind == "type" && Same(model.GetDeclaredSymbol(node), key))
            { DeclarationCount++; return node.WithIdentifier(DeclarationToken(node.Identifier)); }
            return base.VisitDelegateDeclaration(node);
        }
        SyntaxToken DeclarationToken(SyntaxToken token) => SyntaxFactory.Identifier(token.LeadingTrivia, rename.newName, token.TrailingTrivia);
        SyntaxNode AddMovedFrom(SyntaxNode node, INamedTypeSymbol type, string priorName)
        {
            if (type == null || !IsUnityObject(type)) return node;
            string oldNamespace = type.ContainingNamespace.IsGlobalNamespace ? "" : type.ContainingNamespace.ToDisplayString();
            string fromName = rename.kind == "type" ? priorName : type.Name;
            var attribute = ParseAttribute("[global::UnityEngine.Scripting.APIUpdating.MovedFromAttribute(true, " +
                JsonSerializer.Serialize(oldNamespace) + ", " + JsonSerializer.Serialize(assembly) + ", " + JsonSerializer.Serialize(fromName) + ")] ");
            return node switch
            {
                ClassDeclarationSyntax c => c.AddAttributeLists(attribute.WithTrailingTrivia(SyntaxFactory.ElasticCarriageReturnLineFeed)),
                RecordDeclarationSyntax r => r.AddAttributeLists(attribute.WithTrailingTrivia(SyntaxFactory.ElasticCarriageReturnLineFeed)),
                _ => node
            };
        }
        static AttributeListSyntax ParseAttribute(string text) => SyntaxFactory.ParseCompilationUnit(text + "class __BeaverAttributeHost {}")
            .Members.OfType<ClassDeclarationSyntax>().Single().AttributeLists.Single();
        bool HasFormerlySerializedAs(FieldDeclarationSyntax field, string oldName) => field.AttributeLists.SelectMany(x => x.Attributes).Any(attribute =>
            (attribute.Name.ToString().EndsWith("FormerlySerializedAs", StringComparison.Ordinal) ||
             attribute.Name.ToString().EndsWith("FormerlySerializedAsAttribute", StringComparison.Ordinal)) &&
            attribute.ArgumentList?.Arguments.FirstOrDefault()?.Expression is LiteralExpressionSyntax literal &&
            literal.IsKind(SyntaxKind.StringLiteralExpression) && literal.Token.ValueText == oldName);
        public override SyntaxNode VisitFieldDeclaration(FieldDeclarationSyntax node)
        {
            bool selected = rename.kind == "serialized_field" && node.Declaration.Variables.Any(v => Same(model.GetDeclaredSymbol(v), key));
            if (selected && node.Declaration.Variables.Count != 1)
            {
                Unsupported.Add(new { code = "MULTI_VARIABLE_FIELD_UNSUPPORTED", path = node.SyntaxTree.FilePath, span = node.SpanStart });
                return base.VisitFieldDeclaration(node);
            }
            var visited = (FieldDeclarationSyntax)base.VisitFieldDeclaration(node);
            if (!selected || HasFormerlySerializedAs(node, target.Name)) return visited;
            var attribute = ParseAttribute("[global::UnityEngine.Serialization.FormerlySerializedAsAttribute(" + JsonSerializer.Serialize(target.Name) + ")] ");
            return visited.AddAttributeLists(attribute.WithTrailingTrivia(SyntaxFactory.ElasticCarriageReturnLineFeed));
        }
        public override SyntaxNode VisitVariableDeclarator(VariableDeclaratorSyntax node)
        {
            if (rename.kind == "serialized_field" && Same(model.GetDeclaredSymbol(node), key))
            { DeclarationCount++; return node.WithIdentifier(DeclarationToken(node.Identifier)); }
            return base.VisitVariableDeclarator(node);
        }
    }
}
