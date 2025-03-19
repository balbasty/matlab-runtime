function compile_test_runtime()

this_file = mfilename('fullpath');
if ~strcmpi(this_file(end-2:end), '.m')
    this_file = which('compile_test_runtime');
end
this_dir  = getfield(dir(this_file), 'folder');
build_dir = fullfile(this_dir, 'build');

if exist(build_dir, 'dir')
    rmdir(build_dir, 's');
end

mcc('-v', ...                       % Display verbose output
    '-W','python:test_runtime',...  % Build target
    '-G', ...                       % Include debug symbols
    '-K', ...                       % Keep partial output
    '-X', ...                       % Exclude data files
    '-d', build_dir, ...            % Output folder
    'test_runtime' ... 
);

movefile( ...
    fullfile(build_dir, 'test_runtime', 'test_runtime.ctf'), ...
    fullfile(this_dir, '_test_runtime.ctf') ...
);
% rmdir(build_dir, 's');